"""Group profile updates and billing stay isolated from removed private guidance."""

import pytest

from astrbot.builtin_stars.wangshangliao_moderation.conversation_profiles import DEFAULT
from astrbot.builtin_stars.wangshangliao_moderation.open_rpc import OpenCall, OpenRPC
from tests.test_wangshangliao_conversation_profiles import configure, save
from tests.test_wangshangliao_dashboard import headers as headers
from tests.test_wangshangliao_dashboard import page_app as page_app
from tests.test_wangshangliao_group_ai import fake_http, model_scope
from tests.test_wangshangliao_open_rpc_guards import rpc_scope as rpc_scope


@pytest.mark.asyncio
async def test_group_profile_and_billing_remain_independent_of_retired_guidance(
    rpc_scope, monkeypatch
):
    value, adapter, _ = rpc_scope
    configure(value)
    save(value, "group", selection={**DEFAULT, "max_tokens": 333})
    row = await model_scope(value, adapter)
    assert all(profile["kind"] == "group" for profile in value.profiles.rows())
    budget = dict(value.budget)
    await OpenRPC(value).dispatch(
        OpenCall(
            action="ai_profile",
            actor="dashboard/test",
            payload={
                "kind": "group",
                "account": "",
                "group_id": "",
                "version": 1,
                "selection": {**DEFAULT, "max_tokens": 444},
            },
        )
    )
    assert (
        value.profiles.resolve("group", "99", "5")["parameters"][
            "max_completion_tokens"
        ]
        == 444
    )
    assert all(profile["kind"] == "group" for profile in value.profiles.rows())
    assert value.budget == budget
    requests = fake_http(monkeypatch, content="group-only-answer")
    await value.group_ai.run(
        adapter, dict(row), "mention/group-profile/1/1001", "今天有什么新安排"
    )
    assert len(requests) == 1
    payload = requests[0][1]["json"]
    assert payload["max_completion_tokens"] == 444
    assert not {"tools", "tool_choice", "functions"} & payload.keys()
    adapter.send_text.assert_awaited_once()
    assert value.store.usage()["used"] == 1
    assert value.budget == budget
    assert value.context.conversation_manager.db.rows
    assert all(
        "FriendMessage" not in conversation.user_id
        for conversation in value.context.conversation_manager.db.rows.values()
    )
