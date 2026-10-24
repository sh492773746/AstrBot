"""Offline regression coverage for authorization and native submission boundaries."""

import asyncio
import copy
import json
import socket
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import (
    activities,
    activity_store,
    content_rules,
)
from astrbot.builtin_stars.wangshangliao_moderation.policy import handle
from astrbot.core.platform.sources.wangshangliao import automatic, moderation, wire
from astrbot.core.platform.sources.wangshangliao.actions import ActionRequest
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.business import (
    BusinessClient,
    BusinessError,
)
from astrbot.core.platform.sources.wangshangliao.progression_store import (
    ProgressionStore,
)
from astrbot.core.platform.sources.wangshangliao.storage import Ledger


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("real_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for module in (automatic, moderation, activities, activity_store, content_rules):
        monkeypatch.setattr(module, "instance_dir", lambda _: tmp_path)


@pytest_asyncio.fixture
async def bot(tmp_path):
    ledger = Ledger(tmp_path / "messages.sqlite3")
    await ledger.open()
    roster = [
        {"userId": "1", "nimId": "901", "groupRole": "GROUP_ROLE_ADMIN"},
        {
            "userId": "2",
            "nimId": "22",
            "groupRole": "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        },
    ]
    value = SimpleNamespace(
        account="1",
        config={
            "id": "transport-hardening",
            "enable": True,
            "_business_active": True,
            "_business_revision": "base-1",
            "enabled_groups": ["5"],
            "proactive_send": {"enabled": True, "targets": ["5"]},
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "content_rules_since": time.time() - 60,
                "mute_keywords": ["badword"],
                "permissions": {"5": ["mute", "recall", "mute_all"]},
            },
        },
        enabled_groups={"5"},
        groups={"5": "55"},
        members={"5": {"1": "901", "2": "22", "9": "909"}},
        stopping=asyncio.Event(),
        connection_state="online",
        ledger=ledger,
        send_lock=asyncio.Lock(),
        nim_account="901",
        business=SimpleNamespace(deployment=SimpleNamespace(key=lambda _: bytes(32))),
        diagnostics=SimpleNamespace(emit=Mock()),
        nim=SimpleNamespace(
            request=AsyncMock(
                return_value=(200, wire.properties([(7, b"1000000"), (12, b"123")]))
            )
        ),
        get_moderation_members=AsyncMock(
            return_value={"complete": True, "groupMemberInfo": roster}
        ),
    )
    for name in ("execute_action", "send_text", "send_reply_text"):
        setattr(value, name, getattr(WangshangliaoAdapter, name).__get__(value))
    try:
        yield value
    finally:
        await ledger.close()


def http_client(post):
    deployment = SimpleNamespace(
        metadata=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        key=lambda _: bytes(32),
        headers={},
        origin="https://offline.invalid",
    )
    client = BusinessClient(deployment, SimpleNamespace(post=post))
    client.fields[3] = 1
    client.jwt = "synthetic-offline-fixture"
    return client


class Response:
    status = 200
    headers = {}

    def __init__(self):
        self.content = self

    async def iter_chunked(self, size):
        yield b'{"code":0,"data":{}}'

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["/v1/group/get-group-list", "/v1/friend/friend-apply-list"]
)
@pytest.mark.parametrize(
    "envelope,expected,number",
    [
        (
            {"code": 1400, "msg": "未设置二级安全码"},
            "secondary_security_code_required",
            1400,
        ),
        (
            {"code": 1400, "msg": "未设置二级安全码 "},
            "secondary_security_code_required",
            1400,
        ),
        (
            {"code": 1400, "msg": " 未设置二级安全码"},
            "secondary_security_code_required",
            1400,
        ),
        (
            {"code": 1400, "msg": "未设置二级安全碼"},
            "secondary_security_code_required",
            1400,
        ),
        (
            {"code": 1400, "msg": "未设置二级安全码\nsecret provider body"},
            "secondary_security_code_required",
            1400,
        ),
        (
            {"code": 1400, "msg": "Other rejection"},
            "secondary_security_code_required",
            1400,
        ),
        ({"code": 1400, "msg": ""}, "secondary_security_code_required", 1400),
        ({"code": 1400, "msg": None}, "secondary_security_code_required", 1400),
        ({"code": 1400}, "secondary_security_code_required", 1400),
        ({"code": 1401, "msg": "未设置二级安全码"}, "account_clearing", 1401),
        (
            {"code": 1402, "msg": "Other rejection"},
            "secondary_security_unverified",
            1402,
        ),
        ({"code": 1403}, "secondary_security_code_invalid", 1403),
        ({"code": 1404}, "secondary_security_locked", 1404),
        ({"code": 1405}, "secondary_security_phone_required", 1405),
        ({"code": 0, "msg": "未设置二级安全码"}, "response_invalid", 0),
        ({"code": "1400", "msg": "未设置二级安全码"}, "response_shape", 0),
        ({"code": 1400.0, "msg": "未设置二级安全码"}, "response_shape", 0),
    ],
)
async def test_secondary_security_codes_follow_typed_official_enum_not_message(
    route, envelope, expected, number
):
    class ProviderResponse(Response):
        async def iter_chunked(self, size):
            yield json.dumps(envelope, ensure_ascii=False).encode()

    post = Mock(return_value=ProviderResponse())
    client = http_client(post)
    client.mac = b"m" * 16

    with pytest.raises(BusinessError) as raised:
        await client.request(route, {})

    assert str(raised.value) == expected
    assert raised.value.number == number
    assert raised.value.submitted is True
    assert raised.value.retry_after == 0
    assert "未设置" not in str(raised.value) and "secret" not in str(raised.value)
    assert client.jwt == "synthetic-offline-fixture" and client.mac == b"m" * 16
    assert not client.lock.locked()
    post.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_http_lock_rechecks_authority_before_any_mutation(tmp_path, asynchronous):
    state = {"allowed": True}
    post = Mock(side_effect=AssertionError("revoked_mutation_must_not_post"))
    client = http_client(post)
    native_request = client.request
    waiting = asyncio.Event()

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        waiting.set()
        return await native_request(route, params, **kwargs)

    def guard():
        if not state["allowed"]:
            raise wire.ProtocolError("revoked")

    async def async_guard():
        await asyncio.sleep(0)
        guard()

    client.request = request
    await client.lock.acquire()
    task = asyncio.create_task(
        moderation.execute(
            client,
            "fixture",
            "1",
            "http-lock",
            "mute_all",
            5,
            authorize=async_guard if asynchronous else guard,
        )
    )
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        state["allowed"] = False
        client.lock.release()
        with pytest.raises(wire.ProtocolError, match="revoked"):
            await asyncio.wait_for(task, 3)
        post.assert_not_called()
        with closing(moderation.operation_database("fixture")) as db:
            result = json.loads(
                db.execute(
                    "SELECT result FROM operations WHERE id='http-lock'"
                ).fetchone()[0]
            )
        assert result["status"] == "rejected"
        assert result["submitted"] is False
    finally:
        if client.lock.locked():
            client.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_async_http_authority_can_refresh_roster_without_deadlock():
    post = Mock(side_effect=lambda *args, **kwargs: Response())
    client = http_client(post)
    checked = []

    async def guard():
        await client.request("/v1/group/get-group-members", {"groupId": 5})
        checked.append(True)

    assert (
        await asyncio.wait_for(
            client.request("/v1/group/set-group-mute", {"groupId": 5}, authorize=guard),
            3,
        )
        == {}
    )
    assert checked == [True]
    assert [call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list] == [
        "get-group-members",
        "set-group-mute",
    ]
    assert not client.lock.locked()


@pytest.mark.asyncio
async def test_http_accepted_receipt_survives_revocation_during_io():
    state = {"allowed": True}

    def post(*args, **kwargs):
        state["allowed"] = False
        return Response()

    def guard():
        if not state["allowed"]:
            raise wire.ProtocolError("revoked")

    assert (
        await http_client(post).request(
            "/v1/group/set-group-mute", {"groupId": 5}, authorize=guard
        )
        == {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ascii", "unicode", "mentions", "private"])
async def test_segments_bound_complete_serialized_application(bot, monkeypatch, kind):
    target = "5"
    mentions = None
    text = "x" * 5000 if kind == "ascii" else "中\U0001f600" * 1800
    if kind == "mentions":
        text = ""
        mentions = []
        for number in range(150):
            nick = f"Member{number}\U0001f600"
            start = len(text.encode("utf-16-le")) // 2
            text += f"@{nick} "
            mentions.append(
                {
                    "uid": number + 1,
                    "nick": nick,
                    "start": start,
                    "end": len(text.encode("utf-16-le")) // 2,
                }
            )
        text += "x" * 5000
    if kind == "private":
        target = "private/2/MjI"
        await bot.ledger.ingest("1", target, "inbound", {"sender": "2"})
    messages = []
    seal = wire.seal_message

    def capture(key, inner, *args):
        messages.append(wire.ApplicationMessage.FromString(inner.SerializeToString()))
        return seal(key, inner, *args)

    monkeypatch.setattr(wire, "seal_message", capture)
    assert (
        await bot.send_reply_text(target, "long-result", text, mentions=mentions)
        == "accepted"
    )
    assert len(messages) > 1
    assert bot.nim.request.await_count == len(messages)
    assert all(len(inner.SerializeToString()) <= 4096 for inner in messages)
    assert (
        "".join(
            inner.mentions.content.data
            if inner.HasField("mentions")
            else inner.content.data
            for inner in messages
        )
        == text
    )
    assert sum(len(inner.mentions.people) for inner in messages) == len(mentions or [])
    assert (await bot.ledger.receipt("long-result"))["status"] == "accepted"
    before = bot.nim.request.await_count
    assert (
        await bot.send_reply_text(target, "long-result", text, mentions=mentions)
        == "accepted"
    )
    assert bot.nim.request.await_count == before


@pytest.mark.asyncio
async def test_local_encoding_failure_is_not_unknown(bot):
    with pytest.raises(wire.ProtocolError, match="message_limit"):
        await bot.send_text("5", "oversized", "x" * 4096)
    assert await bot.ledger.receipt("oversized") is None
    text = "@Member hello"
    with pytest.raises(wire.ProtocolError, match="mention_text"):
        await bot.send_text(
            "5",
            "invalid-mention",
            text,
            mentions=[{"uid": 22, "nick": "Other", "start": 0, "end": 8}],
        )
    assert (await bot.ledger.receipt("invalid-mention"))["status"] == "rejected"
    bot.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_native_receipt_stays_unknown_and_is_not_replayed(bot):
    bot.nim.request.return_value = (200, wire.properties([]))
    with pytest.raises(wire.ProtocolError, match="receipt_shape"):
        await bot.send_reply_text("5", "unconfirmed", "hello")
    assert (await bot.ledger.receipt("unconfirmed/part/0"))["status"] == "unknown"
    assert (await bot.ledger.receipt("unconfirmed"))["status"] == "unknown"
    assert await bot.send_reply_text("5", "unconfirmed", "hello") == "unknown"
    assert bot.nim.request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_io", [False, True])
async def test_accepted_moderation_is_not_downgraded_by_failed_readback(pre_io):
    failure = BusinessError("reauth_required") if pre_io else TimeoutError()
    if pre_io:
        failure.submitted = False
    client = SimpleNamespace(fields=[0, 0, 0, 1])
    client.request = AsyncMock(
        side_effect=[
            {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]},
            {"noticeId": "9"},
            failure,
        ]
    )
    receipt = await moderation.execute(
        client, "fixture", "1", "notice-readback", "announce", 5, text="notice"
    )
    assert receipt["status"] == "accepted"
    assert receipt["submitted"] is True
    assert (
        await moderation.execute(
            client, "fixture", "1", "notice-readback", "announce", 5, text="notice"
        )
        == receipt
    )
    assert client.request.await_count == 3


@pytest.mark.asyncio
async def test_real_http_readback_preflight_preserves_accepted_mutation():
    posts = []

    def post(url, **kwargs):
        posts.append(url.rsplit("/", 1)[-1])
        client.jwt = ""
        return Response()

    client = http_client(post)
    native_request = client.request

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        return await native_request(route, params, **kwargs)

    client.request = request
    receipt = await moderation.execute(
        client, "fixture", "1", "native-readback", "announce", 5, text="notice"
    )
    assert receipt["status"] == "accepted"
    assert receipt["submitted"] is True
    with closing(moderation.operation_database("fixture")) as db:
        evidence = json.loads(
            db.execute(
                "SELECT result FROM operations WHERE id='native-readback'"
            ).fetchone()[0]
        )
    assert evidence == receipt
    assert (
        await moderation.execute(
            client, "fixture", "1", "native-readback", "announce", 5, text="notice"
        )
        == receipt
    )
    assert posts == ["add-notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rename", "cleanup", "automatic-kick", "announce"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_readback_failure_never_erases_accepted_member_action(action, cancelled):
    failure = (
        asyncio.CancelledError() if cancelled else BusinessError("reauth_required")
    )
    failure.submitted = False
    roster = {
        "groupMemberInfo": [
            {
                "userId": 2,
                "nimId": "22",
                "groupRole": "GROUP_ROLE_MEMBER",
                "accountState": "ACCOUNT_STATE_BAN"
                if action == "cleanup"
                else "ACCOUNT_STATE_GOOD",
            }
        ]
    }
    operation = "automatic-kick/readback" if action == "automatic-kick" else action
    actual_action = "kick" if action == "automatic-kick" else action
    client = SimpleNamespace(fields=[0, 0, 0, 1])
    results = [{"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]}]
    if action != "announce":
        results.append(roster)
    client.request = AsyncMock(side_effect=[*results, {"noticeId": "9"}, failure])

    async def execute():
        return await moderation.execute(
            client,
            "fixture",
            "1",
            operation,
            actual_action,
            5,
            2 if action != "announce" else 0,
            text="new card" if action == "rename" else "notice",
            expected_card="" if action == "rename" else None,
            expected_nim="22" if action != "announce" else None,
        )

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await execute()
    else:
        assert (await execute())["status"] == "accepted"
    with closing(moderation.operation_database("fixture")) as db:
        evidence = json.loads(
            db.execute(
                "SELECT result FROM operations WHERE id=?", (operation,)
            ).fetchone()[0]
        )
    assert evidence["status"] == "accepted"
    assert evidence["submitted"] is True
    assert await execute() == evidence
    assert client.request.await_count == len(results) + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_reauthorization_between_segments_stops_remaining_send(bot, asynchronous):
    state = {"allowed": True}

    def guard():
        if not state["allowed"]:
            raise wire.ProtocolError("revoked")

    async def async_guard():
        await asyncio.sleep(0)
        guard()

    async def send(*args):
        state["allowed"] = False
        return 200, wire.properties([(7, b"1000000"), (12, b"123")])

    bot.nim.request.side_effect = send
    with pytest.raises(wire.ProtocolError, match="revoked"):
        await bot.send_reply_text(
            "5",
            "partial",
            "x" * 9000,
            authorize=async_guard if asynchronous else guard,
        )
    assert bot.nim.request.await_count == 1
    assert (await bot.ledger.receipt("partial/part/0"))["status"] == "accepted"
    assert (await bot.ledger.receipt("partial"))["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["advertising", "revision"])
async def test_progressive_rechecks_original_ad_rule_after_reservation(
    bot, tmp_path, change
):
    bot.config.update(
        _open_groups={
            "5": {"advertising": True, "flood": True, "keywords": ["badword"]}
        },
        _open_revisions={"5": "tenant-1"},
        _open_managers={"5": []},
        _open_native_roles={"5": "GROUP_ROLE_ADMIN"},
    )
    bot.recall_violation = AsyncMock(return_value="accepted")
    bot.execute_moderation = AsyncMock(return_value={"status": "accepted"})
    store = ProgressionStore(tmp_path / "moderation.sqlite3")
    bot._progression_store = store
    await asyncio.to_thread(store.initialize)
    with closing(sqlite3.connect(store.path)) as db, db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES('1','5','2',?,'accepted')",
            [(f"warning-{n}",) for n in range(3)],
        )
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    reserve = store.reserve

    def blocked(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return reserve(*args)

    store.reserve = blocked
    task = asyncio.create_task(
        handle(
            bot,
            "5",
            "123",
            {
                "sender": "2",
                "text": "badword",
                "recall_route": {"time": str(int(time.time() * 1000))},
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if change == "advertising":
            bot.config["_open_groups"]["5"]["advertising"] = False
        else:
            bot.config["_open_revisions"]["5"] = "tenant-2"
        release.set()
        await asyncio.wait_for(task, 3)
        bot.recall_violation.assert_not_awaited()
        bot.execute_moderation.assert_not_awaited()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_automatic_preflight_failure_does_not_quarantine_future_messages(bot):
    bot.config["moderation"]["progressive_mute"] = False
    state = {"native_role": "GROUP_ROLE_MEMBER"}
    mutations = []
    client = SimpleNamespace(fields=[0, 0, 0, 1])

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": state["native_role"]}}]}
        if route == "/v1/group/get-group-members":
            return {
                "groupMemberInfo": [
                    {"userId": 2, "nimId": 22, "groupRole": "GROUP_ROLE_MEMBER"}
                ]
            }
        mutations.append(route)
        await kwargs["authorize"]()
        return {}

    async def execute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        return await moderation.execute(
            client,
            bot.config["id"],
            "1",
            operation,
            action,
            group,
            member,
            minutes=minutes,
            authorize=action_check,
        )

    client.request = request
    bot.execute_moderation = AsyncMock(side_effect=execute)
    first = await automatic.mute_and_escalate(bot, "first", "5", "2", "100")
    assert first["status"] == "rejected"
    assert first["submitted"] is False
    state["native_role"] = "GROUP_ROLE_ADMIN"
    second = await automatic.mute_and_escalate(bot, "second", "5", "2", "101")
    assert second["status"] == "accepted"
    assert second["mute_count"] == 1
    assert mutations == ["/v1/group/set-member-mute"]
    assert bot.execute_moderation.await_count == 2


@pytest.mark.asyncio
async def test_existing_unknown_mute_is_preserved_without_replay(bot):
    with closing(automatic.database(bot)) as db, db:
        db.execute(
            "INSERT INTO automatic_mutes VALUES"
            "('historical','1','5','2','22','old','unknown',0,0)"
        )
    bot.execute_moderation = AsyncMock(return_value={"status": "accepted"})
    result = await automatic.mute_and_escalate(bot, "new", "5", "2", "new")
    assert result["reason"] == "prior_mute_unknown"
    bot.execute_moderation.assert_not_awaited()
    with closing(automatic.database(bot)) as db:
        assert db.execute(
            "SELECT status FROM automatic_mutes WHERE operation='historical'"
        ).fetchone() == ("unknown",)


@pytest.mark.asyncio
async def test_cancellation_before_http_submission_does_not_poison_automatic_mutes(bot):
    bot.config["moderation"]["progressive_mute"] = False
    post = Mock(side_effect=lambda *args, **kwargs: Response())
    client = http_client(post)
    native_request = client.request
    waiting = asyncio.Event()

    async def request(route, params, **kwargs):
        if route == "/v1/group/get-group-list":
            return {"owner": [{"groupId": 5, "me": {"role": "GROUP_ROLE_ADMIN"}}]}
        if route == "/v1/group/get-group-members":
            return {
                "groupMemberInfo": [
                    {"userId": 2, "nimId": 22, "groupRole": "GROUP_ROLE_MEMBER"}
                ]
            }
        waiting.set()
        return await native_request(route, params, **kwargs)

    async def execute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        return await moderation.execute(
            client,
            bot.config["id"],
            "1",
            operation,
            action,
            group,
            member,
            minutes=minutes,
            authorize=action_check,
        )

    client.request = request
    bot.execute_moderation = AsyncMock(side_effect=execute)
    await client.lock.acquire()
    task = asyncio.create_task(
        automatic.mute_and_escalate(bot, "cancelled", "5", "2", "100")
    )
    try:
        await asyncio.wait_for(waiting.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        post.assert_not_called()
        with closing(automatic.database(bot)) as db:
            assert db.execute(
                "SELECT status FROM automatic_mutes WHERE operation='cancelled'"
            ).fetchone() == ("rejected",)
        client.lock.release()
        result = await automatic.mute_and_escalate(bot, "next", "5", "2", "101")
        assert result["status"] == "accepted"
        assert result["mute_count"] == 1
        assert post.call_count == 1
    finally:
        if client.lock.locked():
            client.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class BlockingLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        if self.lock.locked():
            self.waiting.set()
        await self.lock.acquire()

    async def __aexit__(self, *args):
        self.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "draw"])
async def test_lottery_owner_revoked_while_waiting_for_send_lock(bot, operation):
    auth = {"admins_id": ["9"]}
    service = activities.GroupActivities(SimpleNamespace(get_config=lambda _: auth))
    event = SimpleNamespace(
        platform=bot,
        unified_msg_origin="fixture:FriendMessage:1/9",
        message_obj=SimpleNamespace(message_id="123"),
        get_sender_id=lambda: "9",
        get_sender_name=lambda: "offline owner",
    )
    with closing(activity_store.database(bot)) as db, db:
        db.execute(
            "INSERT INTO lottery_settings(account,group_id,prize,contact,duration)"
            " VALUES('1','5','offline prize','offline owner',60)"
        )
    if operation == "draw":
        await service.start(event, "5")
        with closing(activity_store.database(bot)) as db, db:
            identifier = db.execute("SELECT id FROM lotteries").fetchone()[0]
            db.execute("UPDATE lotteries SET ends=0")
        bot.nim.request.reset_mock()
    bot.send_lock = BlockingLock()
    await bot.send_lock.lock.acquire()
    task = asyncio.create_task(
        service.start(event, "5")
        if operation == "start"
        else service.draw(bot, identifier)
    )
    try:
        await asyncio.wait_for(bot.send_lock.waiting.wait(), 3)
        auth["admins_id"] = []
        bot.send_lock.lock.release()
        with pytest.raises(wire.ProtocolError, match="activity_owner_revoked"):
            await asyncio.wait_for(task, 3)
        bot.nim.request.assert_not_awaited()
        with closing(activity_store.database(bot)) as db:
            status = db.execute("SELECT status FROM lotteries").fetchone()[0]
        assert status not in {"open", "finished"}
    finally:
        if bot.send_lock.lock.locked():
            bot.send_lock.lock.release()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_group_specific_revision_invalidates_bound_action(bot):
    bot.config["_open_revisions"] = {"5": "one"}
    intent = ActionRequest.bind(bot, "revision", "mute", 5, member=2)
    bot.execute_moderation = AsyncMock(return_value={"status": "accepted"})
    bot.config["_open_revisions"]["5"] = "two"
    with pytest.raises(wire.ProtocolError, match="action_scope_changed"):
        await bot.execute_action(intent)
    bot.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_revision_is_rechecked_after_async_authorization(bot):
    bot.config["_open_revisions"] = {"5": "one"}
    intent = ActionRequest.bind(bot, "async-revision", "mute", 5, member=2)
    bot.execute_moderation = AsyncMock(return_value={"status": "accepted"})

    async def check():
        await asyncio.sleep(0)
        bot.config["_open_revisions"]["5"] = "two"

    with pytest.raises(wire.ProtocolError, match="action_scope_changed"):
        await bot.execute_action(intent, authorize=check)
    bot.execute_moderation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("wsl_active_policy")
async def test_real_group_dispatch_exposes_authenticated_recall_peer(tmp_path):
    queue = asyncio.Queue()
    adapter = WangshangliaoAdapter(
        {"id": "actor-peer", "account_id": "1", "enabled_groups": ["5"]}, {}, queue
    )
    adapter.connection_state = "online"
    adapter.groups = {"5": "55"}
    adapter.members = {"5": {"1": "901", "2": "22"}}
    adapter.ledger = Ledger(tmp_path / "ingress.sqlite3")
    await adapter.ledger.open()
    worker = None
    try:
        await adapter.ledger.ingest(
            "1",
            "5",
            "123",
            {
                "sender": "2",
                "name": "Member",
                "text": "hello",
                "created_at": 0,
                "mentions": [],
                "transport_peer": "untrusted-alternative",
                "recall_route": {"peer": "22", "client": "client", "time": "1000000"},
            },
        )
        worker = asyncio.create_task(adapter.process_group("5", single=True))
        event = await asyncio.wait_for(queue.get(), 3)
        assert event.message_obj.raw_message["_transport_peer"] == "22"
        event.processing_completion.set_result(None)
        await asyncio.wait_for(worker, 3)
    finally:
        adapter.stopping.set()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await adapter.ledger.close()


@pytest.mark.asyncio
async def test_full_invitation_records_keep_leave_and_apply_events(bot):
    records = []
    for index, event_type in enumerate(
        ("INVITATION", "LEAVE", "APPLY", "INVITATION"), 1
    ):
        records.append(
            {
                "groupId": 5,
                "applyId": str(index),
                "groupApplyType": event_type,
                "state": "MEMBER_STATE_GOOD",
                "applyTime": f"2026-10-01T12:0{index}:00Z",
                "applicant": {"userId": 2, "nimId": 22},
                "inviter": {"userId": 9} if event_type == "INVITATION" else None,
            }
        )
    bot.business.fields = [0, 0, 0, 1]
    bot.business.request = AsyncMock(return_value={"list": records, "lastId": "4"})
    bot.invitation_read_lock = asyncio.Lock()
    bot.invitation_read_at = {}
    result = await WangshangliaoAdapter.get_group_invitation_records(
        bot, "5", pending_only=False
    )
    assert [item["event_type"] for item in result["items"]] == [
        "INVITATION",
        "LEAVE",
        "APPLY",
        "INVITATION",
    ]
    assert [item["apply_id"] for item in result["items"]] == ["1", "2", "3", "4"]
    assert result["scope"] == "account_visible_records"
    assert result["complete"] is False
    assert result["last_id"] == "4"
    bot.invitation_read_at.clear()
    bot.business.request.return_value["list"] = copy.deepcopy(records)
    bot.business.request.return_value["list"][1]["applyTime"] = "not-a-time"
    with pytest.raises(wire.ProtocolError, match="invitation_response"):
        await WangshangliaoAdapter.get_group_invitation_records(
            bot, "5", pending_only=False
        )
