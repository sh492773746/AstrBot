"""Only searched native group members may enter a confirmed speech whitelist."""

import copy
import re
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import ValidationError

from astrbot.builtin_stars.wangshangliao_moderation import member_exemptions
from astrbot.builtin_stars.wangshangliao_moderation.member_whitelist import REQUEST
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from tests.test_wangshangliao_command_menu import leaves, step
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


@pytest_asyncio.fixture
async def whitelist(setup, tmp_path, monkeypatch):
    """Provide mutable native membership without touching production persistence.

    Args:
        setup: Existing two-group command fixture.
        tmp_path: Private SQLite directory.
        monkeypatch: Scoped persistence replacement.

    Returns:
        Service, event and native member rows.
    """
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    value, _, event = setup
    rows = copy.deepcopy(value.roster.return_value)

    async def roster(adapter, group):
        current = copy.deepcopy(rows)
        value.member_evidence[(str(adapter.account), group)] = current
        return current

    value.roster = AsyncMock(side_effect=roster)
    value.verify_actor = AsyncMock()
    value.context.get_provider = AsyncMock()
    return value, event, rows


async def search_preview(value, event):
    """Create an immutable backend draft without the retired chat addition route.

    Args:
        value: Isolated command service.
        event: Fresh message fixture for the authorized manager.

    Returns:
        Explicit confirmation token.
    """
    options = {
        "actor": event.actor_identity.principal,
        "session": value.navigation.key(event),
        "event": event,
    }
    search = await value.member_whitelist.run(
        event.platform, "5", "search", mode="add", query="Target", **options
    )
    assert search["members"] == [{"id": "3", "name": "Target"}]
    preview = await value.member_whitelist.run(
        event.platform,
        "5",
        "preview",
        selection=search["selection"],
        index=1,
        **options,
    )
    return preview["token"]


@pytest.mark.asyncio
async def test_fixed_chat_search_selection_confirmation_and_removal(whitelist):
    value, event, _ = whitelist
    token = await search_preview(value, event)
    instance = event.platform.config["id"]
    assert not member_exemptions.is_exempt(instance, "1", "5", "3", "903")
    reply = await step(value, event, "确认白名单 " + token, native=True)
    assert "已加入" in reply
    assert member_exemptions.is_exempt(instance, "1", "5", "3", "903")
    assert not member_exemptions.is_exempt(instance, "1", "6", "3", "903")
    assert "已加入" not in await step(value, event, "确认白名单 " + token, native=True)
    assert "Target" in await step(value, event, "白名单", native=True)
    assert "Target" in await step(value, event, "移除白名单 Target", native=True)
    reply = await step(value, event, "选择白名单 1", native=True)
    token = re.search(r"确认白名单 ([A-Za-z0-9_-]+)", reply)[1]
    assert "已移出" in await step(value, event, "确认白名单 " + token, native=True)
    assert not member_exemptions.is_exempt(instance, "1", "5", "3", "903")
    assert member_exemptions.read_exemptions(instance, "1", "5") == (2, {})
    value.context.get_provider.assert_not_called()
    event.platform.execute_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "left",
        "peer",
        "name",
        "role",
        "cancel",
        "version",
        "settings",
        "grant",
        "native_role",
    ],
)
async def test_stale_preview_cannot_commit(whitelist, change):
    value, event, rows = whitelist
    token = await search_preview(value, event)
    if change == "left":
        rows.pop("3")
    elif change == "peer":
        rows["3"]["nimId"] = "999"
    elif change == "name":
        rows["3"]["userNick"] = "Another name"
    elif change == "role":
        rows.pop("2")
    elif change == "cancel":
        await step(value, event, "取消", native=True)
    elif change == "version":
        member_exemptions.set_exemption(
            event.platform.config["id"],
            "1",
            "5",
            "2",
            "902",
            enabled=True,
            actor="server/test",
            reason="Concurrent edit",
            guard=lambda: None,
        )
    elif change == "grant":
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    elif change == "native_role":
        rows["2"]["groupRole"] = "GROUP_ROLE_MEMBER"
    else:
        value.settings["version"] += 1
    reply = await step(value, event, "确认白名单 " + token, native=True)
    assert "已加入" not in reply
    assert not member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "3", "903"
    )


@pytest.mark.asyncio
async def test_names_not_uid_and_homonyms_are_selected_individually(whitelist):
    value, event, rows = whitelist
    rows["4"] = {**rows["3"], "userId": "4", "nimId": "904"}
    options = {"actor": "dashboard/offline", "session": "dashboard/offline"}
    result = await value.member_whitelist.run(
        event.platform, "5", "search", mode="add", query="903", **options
    )
    assert not result["members"]
    result = await value.member_whitelist.run(
        event.platform, "5", "search", mode="add", query="Target", **options
    )
    assert {row["id"] for row in result["members"]} == {"3", "4"}
    preview = await value.member_whitelist.run(
        event.platform,
        "5",
        "preview",
        selection=result["selection"],
        index=2,
        **options,
    )
    token = preview["token"]
    await value.member_whitelist.run(
        event.platform, "5", "commit", token=token, **options
    )
    assert member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "4", "904"
    )
    assert not member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "3", "903"
    )


@pytest.mark.asyncio
async def test_add_menu_explains_native_mention_without_opening_a_draft(whitelist):
    value, event, _ = whitelist
    path = next(path for item, path in leaves() if item.command == "添加白名单")
    await step(value, event, "菜单", native=True)
    for number in path:
        reply = await step(value, event, str(number), native=True)
    assert "真正@" in reply and "无需搜索或确认" in reply
    assert not value.member_whitelist.pending
    assert not member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "3", "903"
    )


@pytest.mark.asyncio
async def test_private_add_menu_returns_to_the_target_group(whitelist):
    value, event, _ = whitelist
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.eligible = True
    path = next(path for item, path in leaves() if item.command == "添加白名单")
    await step(value, event, "菜单", native=True)
    for number in path:
        reply = await step(value, event, str(number), native=True)
    assert "真正@" in reply and "目标群" in reply
    assert not value.member_whitelist.pending
    assert not member_exemptions.is_exempt(
        event.platform.config["id"], "1", "5", "3", "903"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["ordinary", "wrong_peer", "wrong_group"])
async def test_whitelist_search_requires_exact_current_management_authority(
    whitelist, permission
):
    value, event, _ = whitelist
    if permission == "ordinary":
        event.get_sender_id = lambda: "3"
        event.actor_identity = replace(
            event.actor_identity,
            claimed_uid="3",
            verified_uid="3",
            peer="903",
        )
    elif permission == "wrong_peer":
        event.actor_identity = replace(event.actor_identity, peer="999")
    else:
        value.management.assignments[value.management.key(event.platform, "5")] = ()
    assert "本群成员搜索结果" not in await step(
        value,
        event,
        "添加白名单 Target",
        native=True,
    )
    assert not value.member_whitelist.pending
    value.context.get_provider.assert_not_called()


@pytest.mark.asyncio
async def test_commit_guard_rejects_last_moment_membership_change(
    whitelist, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import member_whitelist

    value, event, _ = whitelist
    token = await search_preview(value, event)
    write = member_exemptions.set_exemption

    def changed(*args, **kwargs):
        value.member_evidence[("1", "5")]["3"]["nimId"] = "999"
        return write(*args, **kwargs)

    monkeypatch.setattr(member_whitelist, "set_exemption", changed)
    assert "已加入" not in await step(value, event, "确认白名单 " + token, native=True)
    assert member_exemptions.read_exemptions(event.platform.config["id"], "1", "5") == (
        0,
        {},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["chat", "dashboard"])
@pytest.mark.parametrize("phase", ["admission", "commit"])
@pytest.mark.parametrize("change", ["managers", "paused", "epoch", "deleted"])
async def test_persisted_revocation_rejects_confirmation_before_cache_refresh(
    whitelist, monkeypatch, channel, phase, change
):
    from astrbot.builtin_stars.wangshangliao_moderation import member_whitelist

    value, event, _ = whitelist
    adapter = event.platform
    tenant = copy.deepcopy(value.tenants[("1", "5")])
    options = (
        {
            "actor": event.actor_identity.principal,
            "session": value.navigation.key(event),
            "event": event,
        }
        if channel == "chat"
        else {
            "actor": "dashboard/offline",
            "session": "dashboard/offline",
            "tenant_version": tenant["version"],
        }
    )
    workflow = value.member_whitelist
    search = await workflow.run(
        adapter, "5", "search", mode="add", query="Target", **options
    )
    preview = await workflow.run(
        adapter, "5", "preview", selection=search["selection"], index=1, **options
    )
    event.message_obj.message_id += "/confirmation"
    write = member_exemptions.set_exemption

    def changed(*args, **kwargs):
        original_guard = kwargs["guard"]
        checks = 0

        def verify():
            nonlocal checks
            checks += 1
            if checks == (1 if phase == "admission" else 2):
                with value.store.connect(write=True) as db:
                    if change == "deleted":
                        db.execute(
                            "DELETE FROM tenants WHERE account='1' AND group_id='5'"
                        )
                    else:
                        mutation = {
                            "managers": "managers='[]',version=version+1",
                            "paused": "paused=1,version=version+1",
                            "epoch": "epoch=epoch+1",
                        }[change]
                        db.execute(
                            "UPDATE tenants SET "
                            + mutation
                            + " WHERE account='1' AND group_id='5'"
                        )
            original_guard()

        kwargs["guard"] = verify
        return write(*args, **kwargs)

    monkeypatch.setattr(member_whitelist, "set_exemption", changed)
    with pytest.raises(OpenError, match="tenant_changed"):
        await workflow.run(adapter, "5", "commit", token=preview["token"], **options)
    assert value.tenants[("1", "5")] == tenant
    assert member_exemptions.read_exemptions(adapter.config["id"], "1", "5") == (
        0,
        {},
    )


@pytest.mark.asyncio
async def test_rpc_selection_is_bound_to_group_actor_and_version(
    rpc_scope, tmp_path, monkeypatch
):
    value, adapter, rows = rpc_scope
    rows[-1]["userNick"] = "Native Member"
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    rpc = OpenRPC(value)
    scope = {
        "account": "99",
        "group_id": "5",
        "tenant_version": value.tenants[("99", "5")]["version"],
    }

    async def call(operation, actor="dashboard/offline", **args):
        return await rpc.dispatch(
            OpenCall(
                action="member_whitelist",
                actor=actor,
                payload={**scope, "operation": operation, **args},
            )
        )

    result = await call("search", mode="add", query="Native")
    assert result["members"] == [{"id": "2", "name": "Native Member"}]
    with pytest.raises(OpenError):
        await call(
            "preview", actor="dashboard/other", selection=result["selection"], index=1
        )
    with pytest.raises(OpenError):
        await call("preview", group_id="6", selection=result["selection"], index=1)
    preview = await call("preview", selection=result["selection"], index=1)
    with pytest.raises(ValidationError):
        await call("commit", token=preview["token"], member="1002")
    saved = await call("commit", token=preview["token"])
    assert saved["saved"]
    assert member_exemptions.is_exempt(adapter.config["id"], "99", "5", "2", "1002")
    assert not member_exemptions.is_exempt(adapter.config["id"], "99", "6", "2", "1002")
    with pytest.raises(OpenError):
        await call("commit", token=preview["token"])
    assert len((await call("read"))["members"]) == 1
    with pytest.raises(OpenError):
        await call("search", actor="user/2", mode="add", query="Native")


@pytest.mark.parametrize(
    "operation,extra",
    [
        ("search", {"mode": "add", "query": "Name", "member": "123"}),
        ("preview", {"selection": "s" * 24, "index": True}),
        ("preview", {"selection": "s" * 24, "index": 21}),
        ("commit", {"token": "unknown"}),
    ],
)
def test_typed_request_forbids_arbitrary_member_selection(operation, extra):
    with pytest.raises(ValidationError):
        REQUEST.validate_python(
            {
                "account": "1",
                "group_id": "5",
                "tenant_version": 1,
                "operation": operation,
                **extra,
            }
        )


def test_whitelist_optimistic_version_is_transactional(tmp_path, monkeypatch):
    monkeypatch.setattr(member_exemptions, "instance_dir", lambda _: tmp_path)
    assert member_exemptions.read_exemptions("bot", "1", "5") == (0, {})
    kwargs = {
        "enabled": True,
        "actor": "server/test",
        "reason": "Versioned",
        "guard": lambda: None,
    }
    member_exemptions.set_exemption(
        "bot", "1", "5", "2", "902", expected_version=0, **kwargs
    )
    with pytest.raises(ProtocolError, match="member_whitelist_changed"):
        member_exemptions.set_exemption(
            "bot", "1", "5", "3", "903", expected_version=0, **kwargs
        )
    assert member_exemptions.read_exemptions("bot", "1", "5") == (1, {"2": "902"})
