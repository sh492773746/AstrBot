"""Owner-specific support uses synthetic identities and never real model calls."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.consultation_access import (
    consultation_access,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
    ActivationContext,
    recognize_integrated,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.owner_support import TEMPLATE
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_admin_guidance import admin_profile
from tests.test_wangshangliao_conversation_profiles import configure, save
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_integrated_service import service
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope
from tests.test_wangshangliao_public_hardening import chat_event


@pytest.mark.parametrize("text", ["客服列表", "选择客服 1", "退出客服"])
def test_support_selection_is_a_fixed_integrated_command(text):
    assert recognize_integrated(text) == text


def activate(value, account="99", group="10", uid="1", peer="1001", name="Owner"):
    value.store.discover(
        account, {group: {"name": "Offline", "team": "100" + group}}, allow_auto=True
    )
    row = value.store.tenant(account, group)
    code = value.store.mint(account, group, "dashboard/test")
    value.store.activate(
        account,
        group,
        uid,
        code=code,
        epoch=row["epoch"],
        version=row["version"],
        support_owner={"peer": peer, "name": name},
    )
    return value.store.tenant(account, group)


def bind(value, account="99", group="5", uid="1", peer="1001", name="Owner"):
    """Bind an already active synthetic fixture without migrating real groups."""
    row = value.store.tenant(account, group)
    with value.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO support_owners VALUES(?,?,?,1,1)", (uid, peer, name)
        )
        db.execute(
            "INSERT INTO support_group_owners VALUES(?,?,?,?)",
            (account, group, row["epoch"], uid),
        )


def managers(value):
    cfg, _ = configure(value)
    personas, configs, names = {}, {}, {}

    async def create_persona(name, prompt, **kwargs):
        personas[name] = {"name": name, "prompt": prompt, **kwargs}

    async def create_conf(*, config, name):
        key = str(len(configs) + 1)
        configs[key] = config
        names[key] = name
        return key

    value.context.persona_manager = SimpleNamespace(
        get_persona_v3_by_id=personas.get,
        create_persona=AsyncMock(side_effect=create_persona),
        personas_v3=[],
    )
    value.context.astrbot_config_mgr = SimpleNamespace(
        confs=configs,
        create_conf=AsyncMock(side_effect=create_conf),
        get_conf_list=lambda: [{"id": key, "name": names[key]} for key in configs],
    )
    return personas, configs


def test_first_activation_copies_template_then_reuses_across_accounts(tmp_path):
    value = service(tmp_path)
    preview = value.support.preview(
        TEMPLATE,
        0,
        {"wording": "Confirmed template", "knowledge": ["Template facts"]},
        "server",
        "session",
        guard=lambda: None,
    )
    value.support.commit(
        TEMPLATE, preview["token"], "server", "session", guard=lambda: None
    )
    activate(value)
    first = value.support.read("owner:1")
    assert first["config"]["wording"] == "Confirmed template"
    # Later template changes never overwrite a business.
    preview = value.support.preview(
        TEMPLATE,
        1,
        {"wording": "New template"},
        "server",
        "session",
        guard=lambda: None,
    )
    value.support.commit(
        TEMPLATE, preview["token"], "server", "session", guard=lambda: None
    )
    activate(value, account="199", group="11", name="Renamed")
    assert value.support.read("owner:1") == first
    assert len(value.owner_support.rows()) == 1
    assert value.owner_support.available("99")[0]["scope"] == "owner:1"
    assert value.owner_support.available("199")[0]["scope"] == "owner:1"
    activate(value, group="12", uid="2", peer="1002", name="Owner")
    assert value.support.read("owner:2")["config"]["wording"] == "New template"
    assert value.support.read("99")["version"] == 0
    assert not value.group_ai.configs and not value.store.usage()["used"]


@pytest.mark.parametrize("uid,peer", [("1", "fake"), ("2", "1001")])
def test_cross_robot_identity_conflicts_roll_back_activation(tmp_path, uid, peer):
    value = service(tmp_path)
    activate(value)
    with pytest.raises(OpenError, match="identity_conflict"):
        activate(value, account="199", group="11", uid=uid, peer=peer)
    assert not value.store.tenant("199", "11")["active"]
    assert len(value.owner_support.rows()) == 1


@pytest.mark.asyncio
async def test_profile_is_named_after_owner_once_even_with_concurrent_requests(
    tmp_path,
):
    value = service(tmp_path)
    personas, configs = managers(value)
    activate(value, name="Verified owner")
    first, second = await asyncio.gather(
        *(
            value.owner_support.ensure_profile("1", "server", guard=lambda: None)
            for _ in range(2)
        )
    )
    assert first["created"] and not second["created"]
    assert len(configs) == len(personas) == 1
    assert "Verified owner·专属客服·1" in personas
    assert next(iter(personas.values()))["tools"] == []
    activate(value, account="199", group="11")
    await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    assert len(configs) == 1
    assert (
        value.profiles.resolve("consultation", "owner:1")["selection"]["config_id"]
        == "1"
    )


@pytest.mark.asyncio
async def test_existing_dedicated_names_follow_verified_owner_without_new_config(
    tmp_path,
):
    value = service(tmp_path)
    personas, configs = managers(value)
    activate(value, name="Verified owner")
    await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    old_profile = value.profiles.resolve("consultation", "owner:1")
    old_persona = old_profile["persona_id"]
    personas[old_persona]["begin_dialogs"] = ["Question", "Business answer"]

    class SavedConfig(dict):
        def save_config(self):
            """Represent persistence in this isolated in-memory fixture."""

    configs["1"] = SavedConfig(configs["1"])
    manager = value.context.astrbot_config_mgr
    renamed = {}
    manager.update_conf_info = AsyncMock(
        side_effect=lambda key, name: renamed.update({key: name})
    )
    with value.store.connect(write=True) as db:
        db.execute("UPDATE support_owners SET name='Confirmed new name' WHERE uid='1'")
    await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    current = value.profiles.resolve("consultation", "owner:1")
    expected = "Confirmed new name·专属客服·1"
    assert current["persona_id"] == expected
    assert current["selection"]["config_id"] == old_profile["selection"]["config_id"]
    assert personas[expected]["prompt"] == personas[old_persona]["prompt"]
    assert personas[expected]["begin_dialogs"] == personas[old_persona]["begin_dialogs"]
    assert renamed == {"1": expected} and len(configs) == 1
    assert configs["1"]["agent_runner"]["config"]["persona"]["persona_id"] == expected
    versions = current["versions"]
    configs["1"]["agent_runner"]["config"]["persona"]["persona_id"] = old_persona
    await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    assert configs["1"]["agent_runner"]["config"]["persona"]["persona_id"] == expected
    assert value.profiles.resolve("consultation", "owner:1")["versions"] == versions
    assert value.support.read("owner:1")["config"]["links"] == []
    assert not value.group_ai.configs and not value.store.usage()["used"]


@pytest.mark.asyncio
async def test_interrupted_profile_save_reuses_created_native_config(
    tmp_path, monkeypatch
):
    value = service(tmp_path)
    _, configs = managers(value)
    activate(value)
    original = value.profiles.save
    monkeypatch.setattr(
        value.profiles,
        "save",
        lambda *a, **kw: (_ for _ in ()).throw(OpenError("revoked")),
    )
    with pytest.raises(OpenError, match="revoked"):
        await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    monkeypatch.setattr(value.profiles, "save", original)
    await value.owner_support.ensure_profile("1", "server", guard=lambda: None)
    assert len(configs) == 1


@pytest.mark.asyncio
async def test_native_activation_creates_profile_after_policy_generation_refresh(
    rpc_scope,
):
    value, adapter, rows = rpc_scope
    personas, configs = managers(value)
    rows[1]["userNick"] = "Native verified owner"
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET active=0,auto_claim=1,inviter='1',evidence='offline' "
            "WHERE account='99' AND group_id='5'"
        )
    await value.refresh()
    adapter.groups, adapter.group_names = {}, {}
    value.check_inviter = AsyncMock()
    reply = await value.activate(
        ActivationContext(adapter, "1", adapter.config.get("_business_epoch")), "5"
    )
    assert "本群已开通" in reply
    assert value.store.tenant("99", "5")["active"]
    assert len(configs) == 1
    assert "Native verified owner·专属客服·1" in personas
    assert value.profiles.resolve("consultation", "owner:1")["selection"]["config_id"]


@pytest.mark.asyncio
async def test_ambiguous_or_revoked_public_business_never_calls_or_bills(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    configure(value)
    save(value, account="99")
    bind(value)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET managers='[\"2\"]' WHERE account='99' AND group_id='6'"
        )
    bind(value, group="6", uid="2", peer="1002")
    await model_scope(value, adapter)
    requests = fake_http(monkeypatch)
    event = chat_event(adapter)
    await value.chat(event)
    assert not requests and not value.store.usage()["used"]
    assert "客服列表" in event.send.call_args.args[0]
    value.owner_support.state("1", 1, False, "server", guard=lambda: None)
    value.owner_support.state("2", 1, False, "server", guard=lambda: None)
    await value.chat(chat_event(adapter, message="revoked"))
    assert not requests and not value.store.usage()["used"]


@pytest.mark.asyncio
async def test_public_selection_needs_current_list_and_never_grants_editing(rpc_scope):
    value, adapter, _ = rpc_scope
    bind(value)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET managers='[\"2\"]' WHERE group_id='6' AND account='99'"
        )
    bind(value, group="6", uid="2", peer="1002", name="Other")
    event = chat_event(adapter)
    event.actor_identity = ActorIdentity("99", "3", "1003")
    event.get_sender_id = lambda: "3"
    assert value.owner_support.route(event)["ambiguous"]
    with pytest.raises(OpenError, match="selection_changed"):
        await value.owner_support.command(event, "选择客服 1")
    assert "Other" in await value.owner_support.command(event, "客服列表")
    await value.owner_support.command(event, "选择客服 2")
    assert consultation_access(value, event)["support_scope"] == "owner:2"
    assert not value.owner_support.editor(event.actor_identity)
    with value.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET paused=1 WHERE account='99' AND group_id='6'")
    route = value.owner_support.route(event)
    assert route["unavailable"] and not route["target"]
    await value.owner_support.command(event, "退出客服")
    assert value.owner_support.route(event)["target"]["uid"] == "1"


@pytest.mark.asyncio
async def test_new_owner_never_silently_replaces_configured_legacy_business(rpc_scope):
    value, adapter, _ = rpc_scope
    preview = value.support.preview(
        "99", 0, {"knowledge": ["LEGACY_BUSINESS"]}, "server", "s", guard=lambda: None
    )
    value.support.commit("99", preview["token"], "server", "s", guard=lambda: None)
    bind(value)
    event = chat_event(adapter)
    assert value.owner_support.route(event)["ambiguous"]
    listing = await value.owner_support.command(event, "客服列表")
    assert "原有业务客服" in listing
    await value.owner_support.command(event, "选择客服 2")
    assert consultation_access(value, event)["support_scope"] == "99"
    assert value.support.read("owner:1")["config"]["knowledge"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["unverified", "peer", "account", "revoked", "departed", "epoch", "manager"],
)
async def test_owner_editor_matrix(rpc_scope, change):
    value, adapter, _ = rpc_scope
    bind(value)
    await value.roster(adapter, "5")
    identity = ActorIdentity("99", "1", "1001", "1")
    assert value.owner_support.editor(identity)
    if change == "unverified":
        identity = ActorIdentity("99", "1", "1001")
    elif change == "peer":
        identity = ActorIdentity("99", "1", "fake", "1")
    elif change == "account":
        identity = ActorIdentity("199", "1", "1001", "1")
    elif change == "revoked":
        value.owner_support.state("1", 1, False, "server", guard=lambda: None)
    else:
        column, data = {
            "departed": ("present", 0),
            "epoch": ("epoch", 9),
            "manager": ("managers", '["2"]'),
        }[change]
        with value.store.connect(write=True) as db:
            db.execute(
                f"UPDATE tenants SET {column}=? WHERE account='99' AND group_id='5'",
                (data,),
            )
    assert value.owner_support.editor(identity) is None


@pytest.mark.asyncio
async def test_owner_edits_shared_business_but_not_legacy_or_model_settings(rpc_scope):
    value, adapter, _ = rpc_scope
    bind(value)
    await value.roster(adapter, "5")
    event = chat_event(adapter)
    result = await value.support.command(event, "客服话术 Our confirmed wording")
    token = result.split("确认客服 ")[1]
    await value.support.command(event, "确认客服 " + token)
    assert value.support.read("owner:1")["config"]["wording"] == "Our confirmed wording"
    assert value.support.read("99")["version"] == 0
    editor = value.owner_support.editor(event.actor_identity)
    preview = value.support.preview(
        "owner:1",
        1,
        {"wording": "Late"},
        event.actor_identity.principal,
        event.unified_msg_origin,
        editor=editor,
        guard=lambda: None,
    )
    value.owner_support.state("1", 1, False, "server", guard=lambda: None)
    value.owner_support.state("1", 2, True, "server", guard=lambda: None)
    with pytest.raises(OpenError, match="editor_revoked"):
        value.support.commit(
            "owner:1",
            preview["token"],
            event.actor_identity.principal,
            event.unified_msg_origin,
            guard=lambda: None,
        )
    with pytest.raises(ValidationError):
        value.support.preview(
            "owner:1", 1, {"provider_id": "injected"}, "a", "s", guard=lambda: None
        )


@pytest.mark.asyncio
async def test_rules_only_owner_does_not_bypass_ai_trial_boundary(rpc_scope):
    value, adapter, _ = rpc_scope
    configure(value)
    admin_profile(value)
    bind(value)
    await value.roster(adapter, "5")
    with value.store.connect(write=True) as db:
        db.execute("UPDATE tenants SET rules_only=1 WHERE account='99'")
    await value.refresh()
    event = chat_event(adapter)
    await value.verify_actor(event)
    access = consultation_access(value, event)
    assert access["rules_only"] and access.get("dedicated_editor")
    assert not access.get("guidance_trial")
    assert value.support.editor(event.actor_identity) is None
    await value.owner_support.command(event, "客服列表")
    await value.owner_support.command(event, "选择客服 1")
    assert consultation_access(value, event)["rules_only"]


@pytest.mark.asyncio
async def test_selected_business_only_enters_mocked_payload_and_revocation_blocks_late_reply(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    configure(value)
    bind(value)
    save(value, account="owner:1")
    value.owner_support.ensure_profile = AsyncMock()
    await model_scope(value, adapter)
    preview = value.support.preview(
        "owner:1", 0, {"knowledge": ["BUSINESS_ONE"]}, "server", "s", guard=lambda: None
    )
    value.support.commit("owner:1", preview["token"], "server", "s", guard=lambda: None)
    requests = fake_http(monkeypatch, content="Business reply")
    event = chat_event(adapter)
    await value.chat(event)
    assert len(requests) == 1
    assert "BUSINESS_ONE" in json.dumps(requests[0][1]["json"])
    assert "tools" not in requests[0][1]["json"]
    barrier = (asyncio.Event(), asyncio.Event())
    # Keep the second request outside the ordinary per-user cooldown.
    with value.store.connect(write=True) as db:
        db.execute("UPDATE ai_charges SET created=created-60")
    requests = fake_http(monkeypatch, content="Late reply", barrier=barrier)
    event2 = chat_event(adapter, message="second")
    task = asyncio.create_task(value.chat(event2))
    await asyncio.wait_for(barrier[0].wait(), timeout=5)
    value.owner_support.state("1", 1, False, "server", guard=lambda: None)
    barrier[1].set()
    await task
    assert not any("Late reply" == call.args[0] for call in event2.send.call_args_list)


@pytest.mark.asyncio
async def test_typed_server_template_api_has_cas_and_no_arbitrary_config(rpc_scope):
    value, _, _ = rpc_scope
    rpc = OpenRPC(value)
    assert await rpc.dispatch(
        OpenCall(action="owner_support", actor="server", payload={"operation": "list"})
    ) == {"owners": []}
    preview = await rpc.dispatch(
        OpenCall(
            action="owner_support",
            actor="server",
            payload={
                "operation": "preview",
                "uid": "",
                "version": 0,
                "config": {"wording": "Public template"},
            },
        )
    )
    await rpc.dispatch(
        OpenCall(
            action="owner_support",
            actor="server",
            payload={"operation": "commit", "uid": "", "token": preview["token"]},
        )
    )
    assert value.support.read(TEMPLATE)["version"] == 1
    with pytest.raises(OpenError, match="invalid"):
        await rpc.dispatch(
            OpenCall(
                action="owner_support",
                actor="server",
                payload={"operation": "read", "uid": "", "function": "exec"},
            )
        )
