import copy
import importlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.ext import ApplicationHandlerStop

control = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.control")
customer = importlib.import_module("data.plugins.astrbot_plugin_telethon_ai.customer")


def update(actor=1, private=True, data="tgai:token", text="/tgai"):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(
            id=actor, type="private" if private else "group"
        ),
        effective_user=SimpleNamespace(id=actor),
        callback_query=SimpleNamespace(
            data=data, answer=AsyncMock(), edit_message_text=AsyncMock()
        ),
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
    )


def bridge():
    plugin = SimpleNamespace(
        closed=False,
        config={"admin_ids": ["1"]},
        tenants=SimpleNamespace(set_enabled=Mock()),
    )
    return control.Control(plugin)


def test_missing_native_hooks_fails_before_attaching():
    b = bridge()
    b.plugin.context = SimpleNamespace(get_platform_inst=lambda _: SimpleNamespace())
    with pytest.raises(RuntimeError, match="Incompatible AstrBot Telegram adapter"):
        b.bind()
    assert b.platform is None
    assert b.application is None


@pytest.mark.asyncio
async def test_confirmation_actor_group_expiry_and_replay():
    b = bridge()
    b.pending["token"] = (time.time() + 300, "1", 1, "tgaienable", ["tenant"])
    for event in [update(actor=2), update(private=False)]:
        with pytest.raises(ApplicationHandlerStop):
            await b.confirm(event, None)
    b.plugin.tenants.set_enabled.assert_not_called()
    assert "token" in b.pending
    with pytest.raises(ApplicationHandlerStop):
        await b.confirm(update(), None)
    b.plugin.tenants.set_enabled.assert_called_once_with("1", "tenant", True)
    with pytest.raises(ApplicationHandlerStop):
        await b.confirm(update(), None)
    assert b.plugin.tenants.set_enabled.call_count == 1
    b.pending["token"] = (time.time() - 1, "1", 1, "tgaienable", ["tenant"])
    with pytest.raises(ApplicationHandlerStop):
        await b.confirm(update(), None)
    assert b.plugin.tenants.set_enabled.call_count == 1


@pytest.mark.asyncio
async def test_command_only_proposes_change():
    b = bridge()
    event = update(text="/tgaienable tenant")
    with pytest.raises(ApplicationHandlerStop):
        await b.command(event, SimpleNamespace(args=["tenant"]))
    assert len(b.pending) == 1
    b.plugin.tenants.set_enabled.assert_not_called()
    event.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "tgai",
        "tgaiassign",
        "tgaigroup",
        "tgaienable",
        "tgaidisable",
        "tgaigrant",
        "tgaistop",
        "tgaiapprove",
        "tgaireject",
    ],
)
async def test_group_authorization_does_not_grant_platform_privileges(command):
    b = bridge()
    b.plugin.group_control = SimpleNamespace(
        store=SimpleNamespace(binding=lambda *args: {"owner": "2", "public": True})
    )
    event = update(actor=2, text="/" + command)
    with pytest.raises(ApplicationHandlerStop):
        await b.command(event, SimpleNamespace(args=["tenant", "collector"]))
    assert not b.pending
    b.plugin.tenants.set_enabled.assert_not_called()
    event.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_rechecks_actual_bot_and_actor(tmp_path):
    from astrbot.core.config.default import DEFAULT_CONFIG
    from data.plugins.astrbot_plugin_telethon_ai.tenants import Tenants

    store = Tenants(tmp_path / "tenants.db")
    try:
        tenant = store.create_trial("admin", "1", "22", "AIClient_test")
        profile = copy.deepcopy(DEFAULT_CONFIG)
        profile["admins_id"] = []
        profile["disable_builtin_commands"] = True
        profile["provider_settings"]["enable"] = False
        profile["plugin_set"] = [customer.adapter.NAME]
        profile["kb_names"] = []
        profile["dashboard"]["enable"] = False
        manager = SimpleNamespace(
            ucr=SimpleNamespace(
                get_conf_id_for_umop=lambda _: "profile",
                umop_to_conf_id={"AIClient_test::": "profile"},
            ),
            confs={"profile": profile},
        )
        plugin = SimpleNamespace(
            closed=False,
            tenants=store,
            context=SimpleNamespace(
                astrbot_config_mgr=manager, get_config=lambda _: profile
            ),
        )
        handler = customer.Customer(plugin, "AIClient_test")
        application = SimpleNamespace(bot_data={customer.adapter.NAME + ":ready": True})
        handler.platform = SimpleNamespace(
            required_plugin=customer.adapter.NAME,
            config={"telegram_dedicated_reporting": True},
            application=application,
        )
        handler.application = application
        ctx = SimpleNamespace(bot=SimpleNamespace(id=22, username="testbot"))
        for actor in (1, 2):
            event = update(actor=actor, text="/service")
            with pytest.raises(ApplicationHandlerStop):
                await handler.message(event, ctx)
            reply = event.message.reply_text.call_args.args[0]
            assert ("AI 账号服务" in reply) == (actor == 1)
        store.set_enabled("admin", tenant, True)
        event = update(actor=2, text="/pause")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(event, ctx)
        assert store.summary(tenant)["enabled"]
        event = update(actor=1, text="/pause")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(event, ctx)
        assert not store.summary(tenant)["enabled"]
        event = update(actor=1, text="/service")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(
                event, SimpleNamespace(bot=SimpleNamespace(id=99, username="other"))
            )
        assert "仅限" in event.message.reply_text.call_args.args[0]
        profile["kb_names"] = ["unapproved"]
        event = update(actor=1, text="/service")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(event, ctx)
        assert "配置异常" in event.message.reply_text.call_args.args[0]
        profile["kb_names"] = []
        manager.ucr.get_conf_id_for_umop = lambda _: None
        event = update(actor=1, text="/service")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(event, ctx)
        assert "配置异常" in event.message.reply_text.call_args.args[0]
        manager.ucr.get_conf_id_for_umop = lambda _: "profile"
        application.bot_data.clear()
        event = update(actor=1, text="/service")
        with pytest.raises(ApplicationHandlerStop):
            await handler.message(event, ctx)
        assert "配置异常" in event.message.reply_text.call_args.args[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_customer_handler_failure_never_falls_through():
    handler = customer.Customer(SimpleNamespace(), "AIClient_test")
    handler.message = AsyncMock(side_effect=RuntimeError("private upstream detail"))
    with pytest.raises(ApplicationHandlerStop):
        await handler.dispatch(update(text="hello"), None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["callback", "group", "media", "closed"])
async def test_customer_nonmenu_updates_always_stop(kind):
    plugin = SimpleNamespace(closed=kind == "closed")
    handler = customer.Customer(plugin, "AIClient_test")
    event = update(private=kind != "group")
    if kind == "callback":
        event.message = None
    elif kind == "media":
        event.message.text = None
    with pytest.raises(ApplicationHandlerStop):
        await handler.dispatch(event, None)
