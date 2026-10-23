"""Rules-only server admission without invitation, AI or financial authority."""

import copy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation.consultation_access import (
    consultation_access,
)
from astrbot.builtin_stars.wangshangliao_moderation.group_ai import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import (
    DEFAULT_GROUP_RULES,
    DEFAULT_GROUP_TEMPLATE_VERSION,
    DEFAULT_RULES,
    OpenError,
)
from astrbot.core.platform.sources.wangshangliao.group_scope import group_config
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


async def prepare(scope, group="5"):
    value, _, _ = scope
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET active=0,admission='pending',managers='[]' "
            "WHERE account='99' AND group_id=?",
            (group,),
        )
    await value.refresh()
    return {
        "account": "99",
        "group_id": group,
        "version": value.store.tenant("99", group)["version"],
        "manager": "1",
    }


async def invoke(value, payload, *, commit=False, actor="dashboard/offline"):
    return await OpenRPC(value).dispatch(
        OpenCall(
            action="rules_provision" if commit else "rules_provision_preview",
            actor=actor,
            payload=payload,
        )
    )


@pytest.mark.asyncio
async def test_rules_admission_is_atomic_scoped_and_does_not_grant_ai(rpc_scope):
    value, adapter, _ = rpc_scope
    payload = await prepare(rpc_scope)
    other = copy.deepcopy(value.store.tenant("99", "6"))
    preview = await invoke(value, payload)
    assert not value.store.tenant("99", "5")["active"]
    assert preview["mode"] == "rules_only"
    assert await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    ) == {"saved": True, "mode": "rules_only"}
    tenant = value.store.tenant("99", "5")
    assert tenant["active"] == tenant["rules_only"] == 1
    assert tenant["managers"] == ["1"]
    assert tenant["admission"] == "verified"
    assert not tenant["auto_claim"]
    assert tenant["inviter"] == tenant["evidence"] == ""
    assert value.store.tenant("99", "6") == other
    config = value.group_ai.configs["99", "5"]["config"]
    assert not any(config[k] for k in ("enabled", "mentions", "autonomous", "idle"))
    assert not group_config(adapter.config, "5")["moderation"]["semantic"]["enabled"]
    assert "5" in adapter.config["proactive_send"]["targets"]
    assert "recall" in adapter.config["moderation"]["permissions"]["5"]
    assert not value.plans.payer("99", "5")
    with value.store.connect() as db:
        token = db.execute(
            "SELECT digest,used FROM capabilities WHERE kind='rules_provision'"
        ).fetchone()
        assert token["used"] == 1
        assert (
            token["digest"]
            == hashlib.sha256(preview["confirmation"].encode()).hexdigest()
        )
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    with pytest.raises(OpenError, match="tenant_changed"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("manager", ["2", "99", "123", "9999999999"])
async def test_only_real_native_nonbot_managers_are_eligible(rpc_scope, manager):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    with pytest.raises(OpenError):
        await invoke(value, {**payload, "manager": manager})
    assert not value.store.tenant("99", "5")["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("customized", [False, True])
async def test_rules_admission_initializes_template_without_overwriting_custom_rules(
    rpc_scope, customized
):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    before = (
        {**DEFAULT_RULES, "keywords": ["已有定制"]} if customized else DEFAULT_RULES
    )
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET rules=? WHERE account='99' AND group_id='5'",
            (json.dumps(before),),
        )
    await value.refresh()
    preview = await invoke(value, payload)
    assert value.store.tenant("99", "5")["rules"] == before
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    assert value.store.tenant("99", "5")["rules"] == (
        before if customized else DEFAULT_GROUP_RULES
    )
    with value.store.connect() as db:
        detail = json.loads(
            db.execute(
                "SELECT detail FROM audit WHERE action='rules_service_enabled'"
            ).fetchone()[0]
        )
        assert detail["template_version"] == (
            None if customized else DEFAULT_GROUP_TEMPLATE_VERSION
        )
        assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_template_change_invalidates_an_admission_preview(rpc_scope, monkeypatch):
    from astrbot.builtin_stars.wangshangliao_moderation import rules_provisioning

    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET rules=? WHERE account='99' AND group_id='5'",
            (json.dumps(DEFAULT_RULES),),
        )
    await value.refresh()
    preview = await invoke(value, payload)
    monkeypatch.setattr(
        rules_provisioning,
        "DEFAULT_GROUP_RULES",
        {**DEFAULT_GROUP_RULES, "keywords": ["changed"]},
    )
    with pytest.raises(OpenError, match="confirmation_invalid"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )
    assert not value.store.tenant("99", "5")["active"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"rules": {}}, {"mode": "plus"}, {"version": True}])
async def test_provision_payload_is_not_a_generic_config_object(rpc_scope, extra):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    with pytest.raises(ValidationError):
        await invoke(value, {**payload, **extra})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["actor", "group", "expired", "role", "peer", "paused", "tenant"]
)
async def test_preview_is_revoked_by_scope_identity_or_policy_change(rpc_scope, change):
    value, _, rows = rpc_scope
    payload = await prepare(rpc_scope)
    other = await prepare(rpc_scope, "6")
    preview = await invoke(value, payload)
    actor = "dashboard/offline"
    if change == "actor":
        actor = "dashboard/other"
    elif change == "group":
        payload = other
    elif change == "role":
        rows[1]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif change == "peer":
        rows[1]["nimId"] = "9001"
    elif change == "paused":
        value.store.configure(
            value.store.settings()["version"], True, 20, 1000, "dashboard/offline"
        )
        await value.refresh()
    else:
        with value.store.connect(write=True) as db:
            if change == "expired":
                db.execute("UPDATE capabilities SET expires=?", (time.time() - 1,))
            else:
                db.execute(
                    "UPDATE tenants SET version=version+1 "
                    "WHERE account='99' AND group_id='5'"
                )
    with pytest.raises(OpenError):
        await invoke(
            value,
            {**payload, "confirmation": preview["confirmation"]},
            commit=True,
            actor=actor,
        )
    assert not value.store.tenant("99", "5")["active"]
    assert not value.store.tenant("99", "6")["active"]


@pytest.mark.asyncio
async def test_admission_cannot_be_called_from_chat(rpc_scope):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    with pytest.raises(OpenError, match="server_required"):
        await invoke(value, payload, actor="1")


@pytest.mark.asyncio
async def test_revocation_at_commit_rolls_back_admission_and_token(
    rpc_scope, monkeypatch
):
    value, _, rows = rpc_scope
    payload = await prepare(rpc_scope)
    preview = await invoke(value, payload)
    log = value.store.log

    def revoke(db, actor, account, group, action, detail=None):
        log(db, actor, account, group, action, detail)
        if action == "rules_service_enabled":
            value.member_evidence["99", "5"]["1"]["groupRole"] = "GROUP_ROLE_MEMBER"

    monkeypatch.setattr(value.store, "log", revoke)
    with pytest.raises(OpenError, match="manager_native_role_required"):
        await invoke(
            value, {**payload, "confirmation": preview["confirmation"]}, commit=True
        )
    assert not value.store.tenant("99", "5")["active"]
    with value.store.connect() as db:
        assert (
            db.execute(
                "SELECT used FROM capabilities WHERE kind='rules_provision'"
            ).fetchone()[0]
            == 0
        )
        assert not db.execute(
            "SELECT 1 FROM group_ai WHERE account='99' AND group_id='5'"
        ).fetchone()


@pytest.mark.asyncio
async def test_rules_manager_cannot_enable_group_ai_even_from_total_control(rpc_scope):
    value, _, _ = rpc_scope
    payload = await prepare(rpc_scope)
    preview = await invoke(value, payload)
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    with pytest.raises(OpenError, match="group_ai_rules_only"):
        value.group_ai.configure(
            "99",
            "5",
            1,
            value.store.tenant("99", "5")["version"],
            {**DEFAULT, "enabled": True},
            "dashboard/offline",
            control=True,
            guard=lambda: None,
        )


@pytest.mark.asyncio
async def test_rules_only_private_guidance_never_resolves_or_calls_a_model(rpc_scope):
    value, adapter, _ = rpc_scope
    payload = await prepare(rpc_scope)
    preview = await invoke(value, payload)
    await invoke(
        value, {**payload, "confirmation": preview["confirmation"]}, commit=True
    )
    event = SimpleNamespace(
        platform=adapter,
        actor_identity=ActorIdentity("99", "1", "1001", "1"),
        get_sender_id=lambda: "1",
        is_private_chat=lambda: True,
        unified_msg_origin="offline/private",
        message_str="怎么设置关键词？",
        message_obj=SimpleNamespace(message_id="rules-only-question"),
        get_extra=lambda *args: None,
    )
    # This caller is deliberately not authorized for the other test tenant.
    with value.store.connect(write=True) as db:
        db.execute(
            "UPDATE tenants SET managers='[]' WHERE account='99' AND group_id='6'"
        )
    await value.refresh()
    assert consultation_access(value, event)["rules_only"]
    value.budget["confirmed"] = True
    value.reject_reply = AsyncMock()
    value.profiles.resolve = AsyncMock(side_effect=AssertionError("no model profile"))
    await value._chat(event)
    value.reject_reply.assert_awaited_once()
    value.profiles.resolve.assert_not_called()
    assert value.chat_count == 0
    assert not value.chat_users
