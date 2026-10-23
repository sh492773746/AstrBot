"""Scope catalogs and fixed references without network, model calls or authority."""

import json

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from tests.test_wangshangliao_conversation_profiles import configure, save
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_owner_support import bind


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
    save(value, account="99")
    assert value.profiles.inventory() == {
        "configs": [{"id": "default", "name": "Bound WSL", "compatible": True}],
        "personas": [{"id": "support", "name": "support"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,account", [("consultation", "99"), ("admin_guidance", ""), ("group", "")]
)
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
@pytest.mark.parametrize(
    "kind,account", [("consultation", "99"), ("admin_guidance", ""), ("group", "")]
)
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


@pytest.mark.asyncio
async def test_owner_list_filters_robot_and_profile_updates_only_selected_owner(
    rpc_scope,
):
    value, _, _ = rpc_scope
    configure(value)
    bind(value)
    with value.store.connect(write=True) as db:
        db.execute("INSERT INTO support_owners VALUES('2','1002','Other',1,1)")
    save(value, account="owner:1")
    save(value, account="99")
    rpc = OpenRPC(value)
    result = await rpc.dispatch(
        OpenCall(
            action="owner_support",
            actor="dashboard/test",
            payload={"operation": "list", "account": "99"},
        )
    )
    assert [row["uid"] for row in result["owners"]] == ["1"]
    original = value.profiles.resolve("consultation", "99")
    selected = value.profiles.resolve("consultation", "owner:1")["selection"]
    await rpc.dispatch(
        OpenCall(
            action="owner_support",
            actor="dashboard/test",
            payload={
                "operation": "profile",
                "uid": "1",
                "version": 1,
                "selection": {**selected, "temperature": 0.2},
            },
        )
    )
    assert value.profiles.resolve("consultation", "99") == original
    with pytest.raises(OpenError, match="binding_fixed"):
        await rpc.dispatch(
            OpenCall(
                action="owner_support",
                actor="dashboard/test",
                payload={
                    "operation": "profile",
                    "uid": "1",
                    "version": 2,
                    "selection": {**selected, "persona_id": "__builtin__"},
                },
            )
        )
    value.owner_support.state("1", 1, False, "server", guard=lambda: None)
    with pytest.raises(OpenError, match="unavailable"):
        await rpc.dispatch(
            OpenCall(
                action="owner_support",
                actor="dashboard/test",
                payload={
                    "operation": "profile",
                    "uid": "1",
                    "version": 2,
                    "selection": selected,
                },
            )
        )
    assert not value.store.usage()["used"]


def test_guidance_health_detects_stale_manual_without_models_or_mutations(tmp_path):
    value = service(tmp_path)
    configure(value)
    assert not value.support.guidance_status()["profile_ready"]
    save(value, "admin_guidance")
    value.support.sync_guidance_manual("server/test", guard=lambda: None)
    before = value.store.audit()
    status = value.support.guidance_status()
    assert status["profile_ready"] and status["manual_current"]
    assert status["manual_entries"] == status["manual_expected"] > 10
    assert not status["operation_tools"]
    assert value.store.audit() == before
    assert "prompt" not in json.dumps(status)
    with value.store.connect(write=True) as db:
        db.execute("DELETE FROM knowledge_entries WHERE kind='admin_manual'")
    assert not value.support.guidance_status()["manual_current"]
