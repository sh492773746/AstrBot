"""Scoped grants and restoration for opt-in ordinary-account live workflows."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import check_wsl_workflows_live as checks
from scripts.check_wsl_authorization_live import GROUP, RECEIVER, SENDER, USER
from scripts.check_wsl_migration_dialogue import PREFIX


@pytest.fixture
def workflow(monkeypatch):
    bots = [
        {
            "id": instance,
            "revision": "revision-0",
            "proactive_send": {"enabled": True, "targets": ["private/original"]},
            "moderation": {"permissions": {GROUP: ["mute", "unmute", "recall"]}},
        }
        for instance in (RECEIVER, SENDER)
    ]
    before = copy.deepcopy(bots)
    profile = {"admins_id": [USER], "unrelated_setting": "preserved"}
    posts = []

    async def api(method, path, data=None):
        if path == PREFIX + "/settings":
            if method == "GET":
                return {"bots": copy.deepcopy(bots)}
            assert set(data) <= {
                "bot_id",
                "revision",
                "proactive_send",
                "group_id",
                "group",
            }
            assert "policy" not in data
            bot = next(b for b in bots if b["id"] == data["bot_id"])
            assert data["revision"] == bot["revision"]
            if "group" in data:
                assert data["group_id"] == GROUP
                assert set(data["group"]) == {"permissions"}
                bot["moderation"]["permissions"][GROUP] = copy.deepcopy(
                    data["group"]["permissions"]
                )
            bot["proactive_send"] = copy.deepcopy(data["proactive_send"])
            bot["revision"] += "-next"
            posts.append(copy.deepcopy(data))
            return {}
        if path.startswith("/api/v1/config-profiles/"):
            if method == "GET":
                return {"config": copy.deepcopy(profile)}
            profile.update(copy.deepcopy(data))
            return {}
        if path == "/api/v1/bot-types/wangshangliao/registration":
            assert data["action"] == "test_close"
            return {}
        raise AssertionError("Unexpected API route: " + path)

    async def role(enabled):
        assert enabled
        profile["admins_id"] = [v for v in profile["admins_id"] if str(v) != USER] + [
            USER
        ]

    dashboard = SimpleNamespace(
        api=api,
        private="private/test",
        role=role,
        new_window=AsyncMock(),
        online=AsyncMock(),
        send_case=AsyncMock(side_effect=["1. 大海兼职群", "大海兼职群"]),
        save=lambda *args: None,
        window=True,
    )
    probe = SimpleNamespace(
        dashboard=dashboard,
        original_profile=copy.deepcopy(profile),
        result={"pid": "123"},
        output="fixture",
    )
    monkeypatch.setattr(
        checks,
        "rows",
        lambda *args: (
            [{"status": "deleted"}] if args[2].startswith("SELECT status") else []
        ),
    )
    monkeypatch.setattr(
        checks.subprocess, "check_output", lambda *args, **kwargs: "123\n"
    )
    return checks.Workflows(probe), bots, before, profile, posts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["group", "schedule", "schedule_revocation", "schedule_disconnect", "activity"],
)
async def test_workflow_uses_group_patch_and_restores_exact_grants(workflow, case):
    test, bots, before, _, posts = workflow
    await test.prepare(case)
    if case in {"group", "schedule", "schedule_revocation", "schedule_disconnect"}:
        assert set(bots[0]["moderation"]["permissions"][GROUP]) == {
            "mute",
            "unmute",
            "recall",
            "mute_all",
            "unmute_all",
        }
        assert posts[0]["group_id"] == GROUP
    else:
        assert "group" not in posts[0]
    await test.cleanup()
    for index, bot in enumerate(bots):
        assert bot["proactive_send"] == before[index]["proactive_send"]
        assert bot["moderation"] == before[index]["moderation"]
    assert test.result["cleanup"]["grants_exact"]
    assert test.result["cleanup"]["profile_exact"]


@pytest.mark.asyncio
async def test_workflow_restores_a_temporarily_granted_administrator(workflow):
    test, _, _, profile, _ = workflow
    profile["admins_id"] = ["ordinary-owner"]
    test.probe.original_profile = copy.deepcopy(profile)
    await test.prepare("activity")
    assert profile["admins_id"] == ["ordinary-owner", USER]
    await test.cleanup()
    assert profile == test.probe.original_profile


@pytest.mark.asyncio
async def test_workflow_does_not_overwrite_a_concurrent_grant_edit(workflow):
    test, bots, _, _, _ = workflow
    await test.prepare("group")
    bots[0]["proactive_send"]["targets"].append("concurrent-authorized-target")
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert "concurrent-authorized-target" in bots[0]["proactive_send"]["targets"]
    assert test.result["cleanup"]["errors"]


@pytest.mark.asyncio
async def test_failed_muted_probe_still_attempts_an_unmute(workflow):
    test, _, _, _, _ = workflow
    test.group_mute_possible = True
    test.dashboard.send_case.side_effect = None
    await test.cleanup()
    test.dashboard.send_case.assert_awaited_once_with(
        "private", "解除全员禁言", "服务端已接受"
    )
    assert not test.group_mute_possible


@pytest.mark.asyncio
async def test_failed_schedule_cleanup_does_not_skip_group_unmute(workflow):
    test, _, _, _, _ = workflow
    test.schedule_changed = True
    test.group_mute_possible = True
    test.dashboard.send_case.side_effect = [
        RuntimeError("schedule rejected"),
        "accepted",
    ]
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert test.dashboard.send_case.await_args_list[1].args == (
        "private",
        "解除全员禁言",
        "服务端已接受",
    )
    assert not test.group_mute_possible


@pytest.mark.asyncio
@pytest.mark.parametrize("code,accepted", [(802, True), (812, True), (403, False)])
async def test_group_mute_probe_does_not_accept_unrelated_rejection_codes(
    workflow, code, accepted
):
    test, _, _, _, _ = workflow
    test.probe.marker = "fixture"
    test.probe.send = AsyncMock(return_value={"status": "rejected", "code": code})
    if accepted:
        assert (await test.probe_send("muted", muted=True))["code"] == code
    else:
        with pytest.raises(AssertionError):
            await test.probe_send("muted", muted=True)


@pytest.mark.asyncio
async def test_cleanup_restores_revoked_creator_before_schedule_deletion(workflow):
    test, _, _, profile, _ = workflow
    test.revoked_admins = list(profile["admins_id"])
    profile["admins_id"] = []
    test.schedule_changed = True

    async def command(*_):
        assert USER in profile["admins_id"]
        return "已停止"

    test.dashboard.send_case.side_effect = command
    await test.cleanup()
    assert test.revoked_admins is None
    assert not test.schedule_changed
    assert test.result["cleanup"]["profile_exact"]


@pytest.mark.asyncio
async def test_cleanup_does_not_replace_concurrent_admin_edit_after_revocation(
    workflow,
):
    test, _, _, profile, _ = workflow
    test.revoked_admins = list(profile["admins_id"])
    profile["admins_id"] = ["concurrent-administrator"]
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert profile["admins_id"] == ["concurrent-administrator"]
    assert test.result["cleanup"]["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("already_enabled", [False, True])
async def test_cleanup_reconnects_transport_before_deleting_schedule(
    workflow, monkeypatch, already_enabled
):
    test, _, _, _, _ = workflow
    original = {"id": RECEIVER, "type": "wangshangliao", "enable": True}
    saved = {**original, "enable": already_enabled}
    test.transport_before = original
    test.transport_paused = True
    test.schedule_changed = True
    monkeypatch.setattr(
        checks,
        "Path",
        lambda _: SimpleNamespace(
            read_text=lambda **_: json.dumps({"platform": [saved]})
        ),
    )
    original_api = test.dashboard.api
    patches = []

    async def api(method, path, data=None):
        if method == "PATCH":
            assert path == "/api/v1/bots/" + RECEIVER + "/enabled"
            assert data == {"enabled": True}
            patches.append(data)
            saved["enable"] = True
            return {}
        return await original_api(method, path, data)

    async def command(_, text, *__):
        assert saved["enable"] and not test.transport_paused
        test.dashboard.online.assert_awaited_once()
        test.dashboard.new_window.assert_awaited_once()
        return "1. 大海兼职群" if text == "群列表" else "已停止"

    test.dashboard.api = api
    test.dashboard.send_case.side_effect = command
    await test.cleanup()
    assert len(patches) == (0 if already_enabled else 1)
    assert test.result["cleanup"]["transport_exact"]
    assert test.result["cleanup"]["schedule_deleted"]
    assert [call.args[1] for call in test.dashboard.send_case.await_args_list] == [
        "群列表",
        "选择群 1",
        "删除定时",
    ]
    assert not test.schedule_changed


@pytest.mark.asyncio
async def test_cleanup_reports_deleted_transport_without_recreating_it(
    workflow, monkeypatch
):
    test, _, _, _, _ = workflow
    test.transport_before = {"id": RECEIVER, "enable": True}
    test.dashboard.api = AsyncMock(side_effect=test.dashboard.api)
    monkeypatch.setattr(
        checks,
        "Path",
        lambda _: SimpleNamespace(read_text=lambda **_: json.dumps({"platform": []})),
    )
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert test.result["cleanup"]["transport_exact"] is False
    assert (
        "Original transport configuration was not restored"
        in test.result["cleanup"]["errors"]
    )
    assert not any(
        call.args[0] == "PATCH" for call in test.dashboard.api.await_args_list
    )


@pytest.mark.asyncio
async def test_cleanup_does_not_delete_a_schedule_with_an_unverified_group(workflow):
    test, _, _, _, _ = workflow
    test.transport_before = {"id": RECEIVER, "enable": True}
    test.schedule_changed = True
    test.dashboard.send_case.side_effect = ["1. another group"]
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert test.schedule_changed
    test.dashboard.send_case.assert_awaited_once_with("private", "群列表", "大海兼职群")


@pytest.mark.asyncio
async def test_cleanup_requires_deleted_state_not_just_a_success_reply(
    workflow, monkeypatch
):
    test, _, _, _, _ = workflow
    test.schedule_changed = True
    test.dashboard.send_case.side_effect = ["已停止"]
    monkeypatch.setattr(checks, "rows", lambda *args: [{"status": "paused"}])
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert test.schedule_changed
    assert "schedule_deleted" not in test.result["cleanup"]


@pytest.mark.asyncio
async def test_cleanup_does_not_overwrite_concurrent_transport_change(
    workflow, monkeypatch
):
    test, _, _, _, _ = workflow
    test.transport_before = {"id": RECEIVER, "enable": True, "account_id": "1"}
    test.transport_paused = True
    saved = {**test.transport_before, "enable": False, "account_id": "changed"}
    monkeypatch.setattr(
        checks,
        "Path",
        lambda _: SimpleNamespace(
            read_text=lambda **_: json.dumps({"platform": [saved]})
        ),
    )
    original_api = test.dashboard.api

    async def api(method, path, data=None):
        assert method != "PATCH", "Must not change a concurrently edited transport"
        return await original_api(method, path, data)

    test.dashboard.api = api
    with pytest.raises(AssertionError, match="restoration"):
        await test.cleanup()
    assert not saved["enable"] and saved["account_id"] == "changed"
    test.dashboard.online.assert_not_awaited()
    assert test.result["cleanup"]["errors"]
