"""Scope catalogs and fixed references without network, model calls or authority."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_conversation_profiles import configure, save
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


def test_catalog_excludes_unbound_platforms_and_includes_config_inherited_persona(
    tmp_path,
):
    value = service(tmp_path)
    cfg, _ = configure(value)
    value.context.astrbot_config_mgr.confs["telegram"] = cfg
    value.context.astrbot_config_mgr.get_conf_list = lambda: [
        {"id": "default", "name": "Bound WSL"},
        {"id": "telegram", "name": "Other bot"},
    ]
    value.context.persona_manager.personas_v3.append(
        {"name": "telethon-unrelated", "prompt": "DO_NOT_LEAK"}
    )
    assert value.profiles.inventory() == {"configs": [], "personas": []}
    save(value, "group")
    assert value.profiles.inventory() == {
        "configs": [{"id": "default", "name": "Bound WSL", "compatible": True}],
        "personas": [{"id": "support", "name": "support"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,account", [("group", "")])
@pytest.mark.parametrize("key", ["config_id", "persona_id"])
async def test_direct_rpc_cannot_replace_bound_config_or_persona(
    rpc_scope, kind, account, key
):
    value, _, _ = rpc_scope
    cfg, _ = configure(value)
    value.context.astrbot_config_mgr.confs["other"] = cfg
    save(value, kind, account=account)
    before = value.profiles.rows()
    selection = dict(before[0]["selection"])
    selection[key] = "other" if key == "config_id" else "__builtin__"
    with pytest.raises(OpenError, match="ai_profile_binding_fixed"):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="ai_profile",
                actor="dashboard/test",
                payload={
                    "kind": kind,
                    "account": account,
                    "group_id": "",
                    "version": 1,
                    "selection": selection,
                },
            )
        )
    assert value.profiles.rows() == before
    assert not value.store.usage()["used"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,account", [("group", "")])
async def test_rpc_cannot_bootstrap_an_arbitrary_unprovisioned_binding(
    rpc_scope, kind, account
):
    value, _, _ = rpc_scope
    configure(value)
    with pytest.raises(OpenError, match="ai_profile_missing"):
        await OpenRPC(value).dispatch(
            OpenCall(
                action="ai_profile",
                actor="dashboard/test",
                payload={
                    "kind": kind,
                    "account": account,
                    "group_id": "",
                    "version": 0,
                    "selection": value.profiles.resolve("group")["selection"],
                },
            )
        )
    assert not value.profiles.rows()
