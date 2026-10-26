"""Operator ownership, ordinary commands, expiry cleanup and no history replay."""

import copy
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_lajiaoliao_businesses import prepared_plugin, settings
from test_lajiaoliao_plugin_api import call

from data.plugins.astrbot_plugin_lajiaoliao.commerce import CommerceService
from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionError


async def setup(tmp_path, monkeypatch):
    plugin, config, instance = prepared_plugin(tmp_path, monkeypatch)
    config["enable"] = False
    plugin.context.get_config = lambda umo=None: {
        "platform": [config],
        "dashboard": {"username": "alice"},
        "admins_id": ["chat-admin"],
    }
    instance.terminate = AsyncMock()

    async def terminate(identifier):
        await instance.terminate()

    plugin.context.platform_manager = SimpleNamespace(
        reload=AsyncMock(),
        terminate_platform=AsyncMock(
            side_effect=terminate,
        ),
    )
    plugin.context.get_all_providers = lambda: []
    plugin.commerce = CommerceService(plugin)
    plugin.manager.commerce = plugin.commerce
    await plugin.commerce.initialize()
    return plugin, config, instance


def body(action, version=0, **values):
    return {
        "action": action,
        "version": version,
        "confirm": True,
        "request_id": secrets.token_hex(16),
        **values,
    }


@pytest.mark.asyncio
async def test_api_operator_only_idempotency_and_no_credentials(tmp_path, monkeypatch):
    plugin, _, _ = await setup(tmp_path, monkeypatch)
    create = body("create", label="Test customer")
    for owner in ("", "bob", "chat-admin"):
        assert (await call(plugin, "commerce_customers", owner, create))[0] == 403
    status, result = await call(plugin, "commerce_customers", "alice", create)
    assert status == 200
    assert (await call(plugin, "commerce_customers", "alice", create))[1] == result
    status, view = await call(
        plugin, "commerce_customers", "alice", {"action": "status"}
    )
    assert status == 200 and len(view["customers"]) == 1
    assert not any(key in str(view) for key in ("token.json", "password", "api_base"))
    assert (
        await call(
            plugin,
            "commerce_customers",
            "alice",
            body("pause", customer_id=result["customer_id"]),
        )
    )[0] == 409
    await plugin.business.close()


@pytest.mark.asyncio
async def test_management_reports_operator_and_unlicensed_connection(
    tmp_path, monkeypatch
):
    plugin, config, instance = await setup(tmp_path, monkeypatch)
    config["enable"] = True
    instance.connection_state = "online"
    status, result = await call(plugin, "management", "alice", {})
    assert status == 200
    assert result["commerce_operator"] is True
    assert result["instances"][0]["connection_state"] == "subscription_inactive"
    await plugin.business.close()


@pytest.mark.asyncio
async def test_commercial_chat_and_preview_do_not_expose_model_id(
    tmp_path, monkeypatch
):
    plugin, config, _ = prepared_plugin(tmp_path, monkeypatch)
    ref = config["session_ref"]
    plugin.commerce = SimpleNamespace(
        snapshot={"bindings": {config["id"]: {"internal": False}}},
        require=lambda *args, **kwargs: None,
    )
    private_source = "synthetic-private-provider/model"
    await plugin.business.db(
        ref,
        "save_settings",
        "g",
        0,
        settings(ai_management_enabled=True, review_provider_id=private_source),
        "admin",
    )
    event = SimpleNamespace(
        platform=SimpleNamespace(session_ref=ref, config=config),
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        get_sender_id=lambda: "admin",
        get_group_id=lambda: "",
        get_extra=lambda _: None,
        get_platform_name=lambda: "lajiaoliao",
        unified_msg_origin="private",
    )
    plugin.commands.directories[(ref, "admin", "private")] = {
        "selected": "g",
        "expires": time.time() + 600,
    }
    plugin._ai_management_authorized = AsyncMock(return_value=True)
    plugin.context.provider_manager = SimpleNamespace(
        get_provider_by_id=AsyncMock(return_value=object()),
    )
    result = await plugin.commands.execute(event, "规则", "")
    assert private_source not in result and "AI 助手" in result
    preview = json.loads(
        await plugin.private_management(
            event,
            "settings_preview",
            {"changes": {"rankings_enabled": True}},
        )
    )
    assert preview["status"] == "preview"
    assert private_source not in json.dumps(preview)
    assert "review_provider_id" not in preview["before"]
    assert (
        json.loads(
            await plugin.private_management(
                event,
                "settings_preview",
                {"changes": {"review_provider_id": "other"}},
            )
        )["status"]
        == "rejected"
    )
    await plugin.business.close()


@pytest.mark.asyncio
async def test_unassigned_enable_denied_group_limits_and_ownership(
    tmp_path, monkeypatch
):
    plugin, config, _ = await setup(tmp_path, monkeypatch)
    with pytest.raises(SessionError, match="binding"):
        plugin.commerce.validate_config({**config, "enable": True})
    _, created = await call(
        plugin, "commerce_customers", "alice", body("create", label="Customer")
    )
    customer = created["customer_id"]
    request = body(
        "bind",
        1,
        customer_id=customer,
        instance_id=config["id"],
        session_ref=config["session_ref"],
    )
    assert (await call(plugin, "commerce_customers", "bob", request))[0] == 403
    assert (await call(plugin, "commerce_customers", "alice", request))[0] == 200
    too_many = copy.deepcopy(config)
    too_many["message_policy"]["group_ids"] = [str(i) for i in range(11)]
    with pytest.raises(SessionError, match="group_limit"):
        plugin.commerce.validate_config(too_many)
    with pytest.raises(SessionError, match="inactive"):
        plugin.commerce.validate_config({**config, "enable": True})
    await plugin.business.close()


@pytest.mark.asyncio
async def test_expiry_stops_once_preserves_binding_renewal_no_replay(
    tmp_path, monkeypatch
):
    plugin, config, instance = await setup(tmp_path, monkeypatch)
    _, customer = await call(
        plugin, "commerce_customers", "alice", body("create", label="Customer")
    )
    customer = customer["customer_id"]
    assert (
        await call(
            plugin,
            "commerce_customers",
            "alice",
            body(
                "bind",
                1,
                customer_id=customer,
                instance_id=config["id"],
                session_ref=config["session_ref"],
            ),
        )
    )[0] == 200
    _, period = await call(
        plugin,
        "commerce_subscriptions",
        "alice",
        body(
            "activate",
            2,
            customer_id=customer,
            plan="standard",
            paid_confirmed=True,
            receipt_ref="invoice1",
        ),
    )
    config["enable"] = True
    assert plugin.commerce.require(config["id"])
    with pytest.raises(SessionError, match="ai_paused"):
        plugin.commerce.require(config["id"], ai=True)
    # Known pending work is paused, unknown receipts are not cleared.
    await plugin.business.db(config["session_ref"], "settings", "g")
    store = plugin.business.stores[config["session_ref"]]
    with store.connect() as db:
        db.execute(
            "INSERT INTO scheduled_jobs VALUES('job','g','quiet',1,'dashboard:alice',?,'pending','{}','')",
            (time.time() + 60,),
        )
        db.execute(
            "INSERT INTO scheduled_jobs VALUES('unknown','g','quiet',1,'dashboard:alice',?,'unknown','{}','receipt')",
            (time.time(),),
        )
    ledger = plugin.business.stores["__commerce__"]
    with ledger.connect() as db:
        db.execute(
            "UPDATE commerce_periods SET end=? WHERE id=?",
            (time.time() - 1, period["period_id"]),
        )
    await plugin.commerce.refresh()
    await plugin.commerce.lifecycle()
    await plugin.commerce.lifecycle()
    instance.terminate.assert_awaited_once()
    plugin.context.platform_manager.terminate_platform.assert_awaited_once_with(
        config["id"]
    )
    plugin.manager.stop.assert_awaited_once_with(config["session_ref"])
    assert plugin.store._read(config["session_ref"])["state"] == "bound"
    with store.connect() as db:
        assert dict(db.execute("SELECT id,state FROM scheduled_jobs")) == {
            "job": "paused",
            "unknown": "unknown",
        }
    old_stamp = time.time()
    assert (
        await call(
            plugin,
            "commerce_subscriptions",
            "alice",
            body(
                "renew",
                3,
                customer_id=customer,
                plan="standard",
                paid_confirmed=True,
                receipt_ref="invoice2",
            ),
        )
    )[0] == 200
    plugin.context.platform_manager.reload.assert_awaited_once_with(config)
    with pytest.raises(SessionError, match="historical"):
        plugin.commerce.require(config["id"], message_stamp=old_stamp)
    assert plugin.commerce.require(config["id"], message_stamp=time.time() + 1)
    await plugin.business.close()
