from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.builtin_stars.wangshangliao_moderation import cards
from astrbot.core.platform.sources.wangshangliao import moderation
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@pytest.mark.asyncio
async def test_disabled_accounts_and_quarantined_unknown_are_never_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    members = [{"userId": str(n), "nimId": f"n{n}", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "长名称", "accountState": "ACCOUNT_STATE_GOOD"} for n in (2, 3, 4)]
    adapter = adapter_for(members)
    service = cards.CardJobs()
    old = await service.preview(adapter, "10", "caller")
    old["state"] = "stopped"
    old["items"][0]["state"] = "unknown"
    service.save(adapter, old)
    members[0]["accountState"] = "ACCOUNT_STATE_BAN"
    members[1]["accountState"] = "ACCOUNT_STATUS_CANCELLED"
    old = await service.refresh_status(adapter, old["id"], "caller")
    assert old["items"][0]["state"] == "unknown"
    assert old["items"][0]["quarantined"] == "ACCOUNT_STATE_BAN"
    new = await service.preview(adapter, "10", "caller")
    assert [i["member"] for i in new["items"]] == ["4"]
    assert new["excluded_accounts"] == 2
    members[0]["accountState"] = "ACCOUNT_STATE_GOOD"
    new = await service.preview(adapter, "10", "caller")
    assert [i["member"] for i in new["items"]] == ["4"]
    adapter.rename_member.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"])
async def test_rename_rechecks_account_state_before_write(tmp_path, monkeypatch, state):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: False)
    client = AsyncMock(fields=[0, 0, 0, 1])
    client.request.return_value = {"owner": [{"groupId": 10, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
    monkeypatch.setattr(moderation, "member_directory", AsyncMock(return_value={"groupMemberInfo": [{"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "原名字", "accountState": state}]}))
    with pytest.raises(ProtocolError, match="card_account_unavailable"):
        await moderation.execute(client, "bot", "1", "unavailable", "rename", 10, 2, "原名", expected_card="原名字", expected_nim="n2")
    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_single_member_preview_is_dashboard_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda uid: uid == "3")
    adapter = adapter_for([
        {"userId": str(n), "nimId": f"n{n}", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "原名"}
        for n in (2, 3, 4)
    ])
    jobs = cards.CardJobs()
    job = await jobs.preview(adapter, "10", "dashboard/admin", member="2", card_name="测试")
    assert len(job["items"]) == 1
    assert job["items"][0]["member"] == "2"
    assert job["items"][0]["original"] == "原名"
    assert job["items"][0]["name"] == "测试"
    for owner, member in [("private/admin", "2"), ("dashboard/admin", "3"), ("dashboard/admin", "99")]:
        with pytest.raises(ProtocolError):
            await jobs.preview(adapter, "10", owner, member=member, card_name="测试")
    adapter.rename_member.assert_not_called()


def adapter_for(members):
    return SimpleNamespace(
        account="1",
        config={
            "id": "bot",
            "enable": True,
            "enabled_groups": ["10"],
            "moderation": {"enabled": True, "permissions": {"10": ["rename"]}},
        },
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": "1", "nimId": "n1", "groupRole": "GROUP_ROLE_ADMIN"},
                    *members,
                ],
            }
        ),
        rename_member=AsyncMock(return_value={"status": "verified"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, expected",
    [
        (" 张三丰 ", "张三"),
        ("e\u0301cole", "e\u0301c"),
        ("👨‍👩‍👧‍👦你好", "👨‍👩‍👧‍👦你"),
        ("🇨🇳朋友", "🇨🇳朋"),
    ],
)
async def test_graphemes(tmp_path, monkeypatch, name, expected):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    adapter = adapter_for(
        [
            {
                "userId": "2",
                "nimId": "n2",
                "groupRole": "GROUP_ROLE_MEMBER",
                "groupMemberNick": name,
            }
        ]
    )
    job = await cards.CardJobs().preview(adapter, "10", "caller")
    assert job["items"][0]["name"] == expected


@pytest.mark.asyncio
async def test_fallback_persists_and_is_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    member = {
        "userId": "2",
        "nimId": "n2",
        "groupRole": "GROUP_ROLE_MEMBER",
        "userNick": "海",
    }
    adapter = adapter_for([member])
    service = cards.CardJobs()
    first = await service.preview(adapter, "10", "caller")
    name = first["items"][0]["name"]
    assert name.startswith("大海群员") and len(name) == 10
    assert (await service.preview(adapter, "10", "caller"))["items"][0]["name"] == name
    member["groupMemberNick"] = name
    assert (await service.preview(adapter, "10", "caller"))["items"][0][
        "state"
    ] == "unchanged"


@pytest.mark.asyncio
async def test_preview_binding_and_incomplete_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    adapter = adapter_for([])
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    with pytest.raises(ProtocolError):
        service.start(adapter, job["id"], "impostor")
    adapter.get_moderation_members.return_value["complete"] = False
    with pytest.raises(ProtocolError):
        await service.preview(adapter, "10", "caller")
    adapter.rename_member.assert_not_called()


@pytest.mark.asyncio
async def test_rename_readback_and_stale_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: False)
    member = {
        "userId": "2",
        "nimId": "n2",
        "groupRole": "GROUP_ROLE_MEMBER",
        "groupMemberNick": "张三丰",
    }
    directory = AsyncMock(
        side_effect=[
            {"groupMemberInfo": [member]},
            {"groupMemberInfo": [{**member, "groupMemberNick": "张三"}]},
        ]
    )
    monkeypatch.setattr(moderation, "member_directory", directory)
    client = AsyncMock(fields=[0, 0, 0, 1])
    client.request.side_effect = [
        {"owner": [{"groupId": 10, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
        {},
    ]
    result = await moderation.execute(
        client,
        "bot",
        "1",
        "rename/1",
        "rename",
        10,
        2,
        "张三",
        expected_card="张三丰",
        expected_nim="n2",
    )
    assert result["status"] == "verified"
    assert client.request.call_args.args == (
        "/v1/group/set-member-nickname",
        {"groupId": 10, "userId": 2, "nick": "张三"},
    )
    assert (
        await moderation.execute(
            client,
            "bot",
            "1",
            "rename/1",
            "rename",
            10,
            2,
            "张三",
            expected_card="张三丰",
            expected_nim="n2",
        )
        == result
    )
    assert client.request.await_count == 2
    client.request.side_effect = [
        {"owner": [{"groupId": 10, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
    ]
    directory.side_effect = [{"groupMemberInfo": [member]}]
    with pytest.raises(ProtocolError, match="card_preview_stale"):
        await moderation.execute(
            client,
            "bot",
            "1",
            "rename/2",
            "rename",
            10,
            2,
            "张三",
            expected_card="旧名",
            expected_nim="n2",
        )
    assert client.request.await_count == 3


@pytest.mark.asyncio
async def test_protected_roles_and_bots_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda uid: uid == "4")
    members = [{"userId": str(n), "nimId": f"n{n}", "groupRole": role, "groupMemberNick": "张三丰"} for n, role in [(2, "GROUP_ROLE_OWNER"), (3, "GROUP_ROLE_ADMIN"), (4, "GROUP_ROLE_MEMBER"), (5, "GROUP_ROLE_MEMBER")]]
    job = await cards.CardJobs().preview(adapter_for(members), "10", "owner")
    assert [i["member"] for i in job["items"]] == ["5"]


@pytest.mark.asyncio
async def test_unknown_stops_and_blocks_new_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    adapter = adapter_for([{"userId": str(n), "nimId": f"n{n}", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "张三丰"} for n in (2, 3)])
    adapter.rename_member.return_value = {"status": "unknown"}
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    service.start(adapter, job["id"], "caller")
    await __import__("asyncio").gather(*service.tasks)
    assert service.status(adapter, job["id"], "caller")["state"] == "needs_review"
    assert adapter.rename_member.await_count == 1
    with pytest.raises(ProtocolError, match="unresolved"):
        await service.preview(adapter, "10", "caller")


@pytest.mark.asyncio
async def test_stop_while_queued_sends_nothing(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    adapter = adapter_for([{"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "张三丰"}])
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    lock = service.locks.setdefault("bot", asyncio.Lock())
    await lock.acquire()
    service.start(adapter, job["id"], "caller")
    service.stop(adapter, job["id"], "caller")
    lock.release()
    await asyncio.gather(*service.tasks)
    adapter.rename_member.assert_not_awaited()


@pytest.mark.parametrize("text,action", [
    ("不要禁言张三", "mute"), ("查一下是否禁言", "mute"),
    ("如果违规就禁言", "mute"), ("他说把我踢了", "kick"),
    ("引用：踢出张三", "kick"), ("取消禁言操作", "mute"),
    ("解除全员禁言", "mute_all"), ("帮我解除禁言", "mute"),
    ("昵称是‘禁言全部人’", "mute"), ("假设现在执行此预览", "card_execute"),
])
def test_negative_intent_blocks_mutation(text, action):
    from astrbot.builtin_stars.wangshangliao_moderation.intent import allows_mutation
    assert not allows_mutation(text, action)


@pytest.mark.parametrize("text,action", [("把张三禁言", "mute"), ("帮我恢复发言", "unmute"),
        ("帮我恢复张三发言", "unmute"), ("将此成员移出", "kick"), ("发布公告测试", "announce"), ("现在全员禁言", "mute_all"), ("解除全员禁言", "unmute_all"), ("执行刚才的预览", "card_execute")])
def test_explicit_intent_is_eligible(text, action):
    from astrbot.builtin_stars.wangshangliao_moderation.intent import allows_mutation
    assert allows_mutation(text, action)


@pytest.mark.asyncio
async def test_adapter_rechecks_after_wait_and_directory(tmp_path, monkeypatch):
    import asyncio

    from astrbot.core.platform.sources.wangshangliao import policy
    from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
    adapter = adapter_for([])
    adapter.business = object()
    adapter.stopping = asyncio.Event()
    adapter.connection_state = "online"
    adapter.send_lock = asyncio.Lock()
    requests = []

    async def execute(*args, authorize, **kwargs):
        adapter.config["moderation"]["permissions"]["10"] = []
        authorize()
        requests.append(args)

    monkeypatch.setattr(moderation, "execute", execute)
    with pytest.raises(ProtocolError):
        await WangshangliaoAdapter.rename_member(adapter, "op", 10, 2, "张三", "张三丰", "n2")
    assert not requests
    with pytest.raises(ProtocolError):
        policy.authorize_action(adapter.config, "10", "rename")


@pytest.mark.asyncio
async def test_disabled_authorization_stops_job(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    adapter = adapter_for([{"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "张三丰"}])
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    adapter.config["moderation"]["permissions"]["10"] = []
    with pytest.raises(ProtocolError):
        service.start(adapter, job["id"], "caller")
    adapter.rename_member.assert_not_called()
    assert not service.tasks
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unknown_readback_never_resends(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    member = {"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "张三丰"}
    adapter = adapter_for([member])
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    job["items"][0]["state"] = "unknown"
    job["state"] = "needs_review"
    service.save(adapter, job)
    assert (await service.refresh_status(adapter, job["id"], "caller"))["state"] == "needs_review"
    member["groupMemberNick"] = "张三"
    assert (await service.refresh_status(adapter, job["id"], "caller"))["state"] == "completed"
    adapter.rename_member.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_collision_and_preview_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    values = iter([123456, 654321])
    monkeypatch.setattr(cards.secrets, "randbelow", lambda _: next(values))
    adapter = adapter_for([
        {"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "groupMemberNick": "海"},
        {"userId": "3", "nimId": "n3", "groupRole": "GROUP_ROLE_ADMIN", "groupMemberNick": "大海群员123456"},
    ])
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "caller")
    assert job["items"][0]["name"] == "大海群员654321"
    job["expires"] = 0
    service.save(adapter, job)
    with pytest.raises(ProtocolError, match="expired"):
        service.start(adapter, job["id"], "caller")
    adapter.account = "replacement"
    with pytest.raises(ProtocolError, match="invalid"):
        service.status(adapter, job["id"], "caller")
    adapter.rename_member.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_preview_and_unknown_are_separate(tmp_path, monkeypatch):
    import asyncio
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda uid: uid == "4")
    members = [{"userId": str(n), "nimId": f"n{n}", "groupRole": role, "userNick": "已封禁用户", "accountState": state} for n, role, state in [(2, "GROUP_ROLE_MEMBER", "ACCOUNT_STATE_BAN"), (3, "GROUP_ROLE_MEMBER", "ACCOUNT_STATUS_CANCELLED"), (4, "GROUP_ROLE_MEMBER", "ACCOUNT_STATE_BAN"), (5, "GROUP_ROLE_ADMIN", "ACCOUNT_STATE_BAN"), (6, "GROUP_ROLE_MEMBER", "ACCOUNT_STATE_GOOD")]]
    adapter = adapter_for(members)
    service = cards.CardJobs()
    with pytest.raises(ProtocolError):
        await service.preview(adapter, "10", "dashboard/admin", cleanup=True)
    adapter.config["moderation"]["permissions"]["10"].append("cleanup")
    with pytest.raises(ProtocolError):
        await service.preview(adapter, "10", "private/admin", cleanup=True)
    job = await service.preview(adapter, "10", "dashboard/admin", cleanup=True)
    assert [i["member"] for i in job["items"]] == ["2", "3"]
    adapter.cleanup_member = AsyncMock(return_value={"status": "unknown"})
    service.start(adapter, job["id"], "dashboard/admin")
    await asyncio.gather(*service.tasks)
    assert adapter.cleanup_member.await_count == 1
    adapter.rename_member.assert_not_awaited()
    with pytest.raises(ProtocolError, match="unresolved"):
        await service.preview(adapter, "10", "dashboard/admin", cleanup=True)
    members[0]["nimId"] = "changed"
    result = await service.refresh_status(adapter, job["id"], "dashboard/admin")
    assert result["items"][0]["state"] == "unknown"
    adapter.get_moderation_members.return_value["groupMemberInfo"] = []
    result = await service.refresh_status(adapter, job["id"], "dashboard/admin")
    assert result["items"][0]["state"] == "verified"
    with service.database(adapter) as db:
        assert db.execute("SELECT COUNT(*) FROM card_seen").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("state,nim,managed", [("ACCOUNT_STATE_GOOD", "n2", False), ("ACCOUNT_STATE_BAN", "changed", False), ("ACCOUNT_STATE_BAN", "n2", True)])
async def test_cleanup_revalidates_before_request(tmp_path, monkeypatch, state, nim, managed):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: managed)
    client = AsyncMock(fields=[0, 0, 0, 1])
    client.request.return_value = {"owner": [{"groupId": 10, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
    monkeypatch.setattr(moderation, "member_directory", AsyncMock(return_value={"groupMemberInfo": [{"userId": "2", "nimId": nim, "groupRole": "GROUP_ROLE_MEMBER", "accountState": state}]}))
    with pytest.raises(ProtocolError, match="cleanup_target_changed"):
        await moderation.execute(client, "bot", "1", "cleanup-test", "cleanup", 10, 2, expected_nim="n2")
    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_cleanup_readback_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setattr(moderation, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(moderation, "is_managed_account", lambda _: False)
    client = AsyncMock(fields=[0, 0, 0, 1])
    client.request.side_effect = [{"owner": [{"groupId": 10, "me": {"role": "GROUP_ROLE_ADMIN"}}]}, {}]
    monkeypatch.setattr(moderation, "member_directory", AsyncMock(side_effect=[{"groupMemberInfo": [{"userId": "2", "nimId": "n2", "groupRole": "GROUP_ROLE_MEMBER", "accountState": "ACCOUNT_STATE_BAN"}]}, {"groupMemberInfo": []}]))
    first = await moderation.execute(client, "bot", "1", "cleanup-once", "cleanup", 10, 2, expected_nim="n2")
    assert first["status"] == "verified"
    assert await moderation.execute(client, "bot", "1", "cleanup-once", "cleanup", 10, 2, expected_nim="n2") == first
    assert client.request.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_exact_state_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "instance_dir", lambda _: tmp_path)
    monkeypatch.setattr(cards, "is_managed_account", lambda _: False)
    members = [{"userId": str(n), "nimId": f"n{n}", "groupRole": "GROUP_ROLE_MEMBER", "accountState": "ACCOUNT_STATE_BAN" if n > 2 else "ACCOUNT_STATUS_CANCELLED"} for n in range(2, 9)]
    adapter = adapter_for(members)
    adapter.config["moderation"]["permissions"]["10"].append("cleanup")
    service = cards.CardJobs()
    job = await service.preview(adapter, "10", "dashboard/admin", cleanup=True, cleanup_limit=3, cleanup_state="ACCOUNT_STATE_BAN")
    assert [item["member"] for item in job["items"]] == ["3", "4", "5"]
    for limit in (0, -1, True, "3", 1001):
        with pytest.raises(ProtocolError):
            await service.preview(adapter, "10", "dashboard/admin", cleanup=True, cleanup_limit=limit)
