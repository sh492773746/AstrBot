"""Offline managed-account flood regressions through committed native ingress."""

import asyncio
import json
import socket
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

RECEIVER = "43506527"
SENDER = "23691273"
THIRD = "31846829"
GROUP = "1459282"
OTHER_GROUP = "1516387"
MARKER = "WSL_TEST_ingress_"


@pytest_asyncio.fixture
async def three_rule_ingress(tmp_path, monkeypatch):
    """Build fresh account, policy, window and ledger state without live IO.

    Args:
        tmp_path: Test-owned root for all configuration and database paths.
        monkeypatch: Restores environment and process globals after each case.

    Yields:
        Isolated adapters and the real ingress and moderation implementations.
    """
    # Core imports initialize configuration, so redirect them before importing.
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path))
    monkeypatch.setenv("ASTRBOT_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("ASTRBOT_DISABLE_METRICS", "1")
    monkeypatch.delenv("ASTRBOT_WSL_OPEN", raising=False)
    for operation in ("connect", "connect_ex"):
        monkeypatch.setattr(
            socket.socket,
            operation,
            Mock(side_effect=AssertionError("Network IO is forbidden")),
        )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(side_effect=AssertionError("DNS IO is forbidden")),
    )

    import astrbot.core as core
    from astrbot.builtin_stars.wangshangliao_moderation import (
        content_rules,
        policy,
        settings,
    )
    from astrbot.builtin_stars.wangshangliao_moderation.flood import FloodWindows
    from astrbot.builtin_stars.wangshangliao_moderation.open_service import (
        recognize_integrated,
    )
    from astrbot.core.platform.sources.wangshangliao import (
        event,
        runtime_policy,
        test_rules,
        wire,
    )
    from astrbot.core.platform.sources.wangshangliao.adapter import (
        WangshangliaoAdapter,
    )
    from astrbot.core.platform.sources.wangshangliao.group_scope import scoped
    from astrbot.core.platform.sources.wangshangliao.storage import Ledger
    from astrbot.core.platform.sources.wangshangliao.test_window import (
        TestWindow as Window,
    )
    from tests.fixtures.wsl_actions import bind_action_port

    platforms = [
        {
            "type": "wangshangliao",
            "id": f"offline-{account}",
            "account_id": account,
            "enable": True,
        }
        for account in (RECEIVER, SENDER, THIRD)
    ]
    monkeypatch.setattr(core, "astrbot_config", {"platform": platforms})
    isolated_runtime = runtime_policy.PolicyRuntime()
    monkeypatch.setattr(runtime_policy, "runtime", isolated_runtime)
    monkeypatch.setattr(settings, "runtime", isolated_runtime)
    business = settings.BusinessSettings(None, tmp_path / "settings")
    business.active = True
    isolated_runtime.service = business
    isolated_runtime.classifier = recognize_integrated
    isolated_runtime.observer = business.observe
    calls = []
    sender = SimpleNamespace(
        account=SENDER,
        nim_account="900002",
        config={"id": f"offline-{SENDER}"},
        stopping=asyncio.Event(),
        test_window=None,
    )
    third = SimpleNamespace(
        account=THIRD,
        nim_account="900003",
        config={"id": f"offline-{THIRD}"},
        stopping=asyncio.Event(),
        test_window=None,
    )
    ledger = Ledger(tmp_path / "messages.sqlite3")
    receiver = SimpleNamespace(
        account=RECEIVER,
        nim_account="900001",
        enabled_groups=[GROUP, OTHER_GROUP],
        config={
            **platforms[0],
            "enabled_groups": [GROUP, OTHER_GROUP],
            "_business_active": True,
            "_business_epoch": 7,
            "_business_revision": f"{RECEIVER}:1",
            "_open_integrated": True,
            "_open_groups": {
                group: {"advertising": False, "flood": True, "keywords": []}
                for group in (GROUP, OTHER_GROUP)
            },
            "_open_revisions": {
                group: f"{RECEIVER}/{group}/1/1" for group in (GROUP, OTHER_GROUP)
            },
            "_open_managers": {GROUP: [], OTHER_GROUP: []},
            "_open_native_roles": dict.fromkeys(
                (GROUP, OTHER_GROUP), "GROUP_ROLE_ADMIN"
            ),
            "moderation": {
                "enabled": True,
                "automation_enabled": True,
                "flood_enabled": True,
                "progressive_mute": True,
                "recall_enabled": True,
                "content_rules_since": time.time() - 60,
                "permissions": {
                    group: ["mute", "recall"] for group in (GROUP, OTHER_GROUP)
                },
            },
        },
        ledger=ledger,
        members={
            group: {SENDER: sender.nim_account, THIRD: third.nim_account}
            for group in (GROUP, OTHER_GROUP)
        },
        groups={GROUP: "9100001", OTHER_GROUP: "9100002"},
        stopping=asyncio.Event(),
        connection_state="online",
        send_lock=asyncio.Lock(),
        diagnostics=SimpleNamespace(emit=Mock(), observe=Mock()),
        nim=SimpleNamespace(request=AsyncMock()),
        get_moderation_members=AsyncMock(
            return_value={
                "complete": True,
                "groupMemberInfo": [
                    {"userId": RECEIVER, "groupRole": "GROUP_ROLE_ADMIN"},
                    {
                        "userId": SENDER,
                        "nimId": sender.nim_account,
                        "groupRole": "GROUP_ROLE_MEMBER",
                    },
                    {
                        "userId": THIRD,
                        "nimId": third.nim_account,
                        "groupRole": "GROUP_ROLE_MEMBER",
                    },
                ],
            }
        ),
        calls=calls,
        settings=business,
    )
    window = Window(receiver, sender, [GROUP], ["group_rules"], [MARKER], 60, 10)
    receiver.test_window = window
    # Put the receiver last to exercise account lookup, not positional selection.
    isolated_runtime.open_service = SimpleNamespace(
        adapters=lambda: [third, sender, receiver],
        active=True,
        integrated=True,
        settings={"paused": False},
    )

    async def mute(
        operation, action, group, member, text="", *, minutes=1, action_check=None
    ):
        if action_check:
            await action_check()
        assert (action, str(group), str(member)) == ("mute", GROUP, SENDER)
        calls.append((action, str(member), minutes))
        return {
            "status": "accepted",
            "action": action,
            "group": group,
            "member": member,
        }

    async def recall(service, command, encoded):
        assert (service, command) == (7, 13)
        properties, _ = wire.read_properties(encoded)
        assert properties[2].decode() == receiver.groups[GROUP]
        assert properties[3].decode() == sender.nim_account
        calls.append(("recall", properties[11].decode(), properties[3].decode()))
        return 200, b""

    receiver.execute_moderation = AsyncMock(side_effect=mute)
    receiver.nim.request.side_effect = recall
    receiver.recall_violation = WangshangliaoAdapter.recall_violation.__get__(receiver)
    bind_action_port(receiver)
    assert all(
        event.is_managed_account(account) for account in (RECEIVER, SENDER, THIRD)
    )
    assert isinstance(business.flood_windows, FloodWindows)
    await ledger.open()
    try:
        yield SimpleNamespace(
            receiver=receiver,
            sender=sender,
            third=third,
            window=window,
            Window=Window,
            runtime=isolated_runtime,
            PolicySnapshot=runtime_policy.PolicySnapshot,
            content_rules=content_rules,
            policy=policy,
            test_rules=test_rules,
            scoped=scoped,
        )
    finally:
        await ledger.close()


async def ingest(harness, mid, text, *, group=GROUP, sender=SENDER, peer=None):
    """Persist a native envelope enriched exclusively by the real observer.

    Args:
        harness: Isolated three-account fixture.
        mid: Stable numeric upstream message ID.
        text: Native message text.
        group: Enabled business group ID.
        sender: Native business sender ID.
        peer: Optional explicit native source, including an empty invalid source.

    Returns:
        The committed payload read back from the temporary ledger.
    """
    bot = harness.receiver
    if peer is None:
        peer = (
            harness.sender.nim_account
            if sender == SENDER
            else harness.third.nim_account
        )
    envelope = {
        "sender": sender,
        "text": text,
        "recall_route": {
            "time": str(int(time.time() * 1000)),
            "client": f"client-{mid}",
            "peer": peer,
        },
    }
    snapshot = harness.PolicySnapshot(
        bot.account,
        bot.config["_business_revision"],
        json.dumps(
            {
                "enabled_groups": bot.config["enabled_groups"],
                "moderation": bot.config["moderation"],
                **{
                    key: value
                    for key, value in bot.config.items()
                    if key.startswith("_open_")
                },
            }
        ),
        0,
    )

    def observe(payload, received_at):
        bot.settings.observe(snapshot, bot.account, group, mid, payload, received_at)

    assert await bot.ledger.ingest(bot.account, group, mid, envelope, observe=observe)
    assert "_ingress_flood" not in envelope
    async with bot.ledger.db.execute(
        "SELECT payload FROM inbox WHERE account=? AND team=? AND message=?",
        (bot.account, group, mid),
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return json.loads(row[0])


async def handle(harness, mid, payload, *, group=GROUP):
    """Run the production category, policy and action-port chain.

    Args:
        harness: Isolated three-account fixture.
        mid: Trigger message ID.
        payload: Committed native envelope.
        group: Business group ID.

    Returns:
        Whether flood enforcement consumed the message.
    """
    category = harness.content_rules.flood_category(
        harness.scoped(harness.receiver, group), group, mid, payload
    )
    if not category:
        return False
    return await harness.policy.handle(
        harness.receiver,
        group,
        mid,
        payload,
        assessment={"decision": "violation", "category": category, "message_id": mid},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,same", [(5, True), (5, False)], ids=["5-repeats", "5-in-3"]
)
async def test_valid_window_mutes_before_recalling_every_ingress_message(
    three_rule_ingress, count, same, monkeypatch, tmp_path
):
    harness = three_rule_ingress
    bot, window = harness.receiver, harness.window
    from astrbot.core.platform.sources.wangshangliao import automatic
    from astrbot.core.platform.sources.wangshangliao.progression_store import (
        ProgressionStore,
    )

    monkeypatch.setattr(automatic, "instance_dir", lambda _: tmp_path)
    ProgressionStore(tmp_path / "moderation.sqlite3").initialize()
    with automatic.database(bot) as db:
        db.executemany(
            "INSERT INTO progressive_warnings VALUES(?,?,?,?,?)",
            [(RECEIVER, GROUP, SENDER, f"prior-{n}", "accepted") for n in range(3)],
        )
    assert window.active()
    assert window.accepted == {}
    messages = [str(100 + n) for n in range(count)]
    for n, mid in enumerate(messages):
        payload = await ingest(
            harness, mid, MARKER + ("repeat" if same else f"item-{n}")
        )
        assert window.accepted[(GROUP, mid)] == "group_rules"
        assert window.remaining == 10 - n - 1
        assert harness.test_rules.rule_target(bot, GROUP, SENDER, mid)
        assert harness.test_rules.rule_target(
            harness.scoped(bot, GROUP), GROUP, SENDER, mid
        )
        evidence = payload["_ingress_flood"]
        assert evidence["live"] is True
        assert evidence["account"] == RECEIVER
        assert evidence["group"] == GROUP
        assert evidence["sender"] == SENDER
        assert evidence["message"] == mid
        assert evidence["revision"] == bot.config["_open_revisions"][GROUP]
        assert evidence["count"] == n + 1
        assert evidence["repeated"] == (n + 1 if same else 1)
        if n < count - 1:
            assert evidence["category"] == ""
            assert not await handle(harness, mid, payload)
            assert bot.calls == []
    assert evidence["category"] == "message_flood"
    assert evidence["messages"] == messages
    assert await handle(harness, mid, payload)
    assert bot.calls == [("mute", SENDER, 1)] + [
        ("recall", message, harness.sender.nim_account) for message in messages
    ]
    assert bot.execute_moderation.await_count == 1
    assert bot.nim.request.await_count == count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,same", [(5, True), (8, False)], ids=["5-repeats", "8-in-10"]
)
@pytest.mark.parametrize(
    "condition",
    [
        "expired",
        "closed",
        "wrong_group",
        "missing_marker",
        "different_marker",
        "wrong_peer",
        "missing_peer",
        "membership_peer_changed",
        "wrong_scope",
        "third_account",
        "epoch_changed",
        "receiver_not_registered",
        "service_absent",
    ],
)
async def test_unadmitted_managed_bursts_remain_protected(
    three_rule_ingress, condition, count, same
):
    harness = three_rule_ingress
    bot = harness.receiver
    group, sender, peer, marker = GROUP, SENDER, harness.sender.nim_account, MARKER
    if condition == "expired":
        harness.window.expires = 0
    elif condition == "closed":
        bot.test_window = None
    elif condition == "wrong_group":
        # This other group is enabled and authorized, but outside the window.
        group = OTHER_GROUP
    elif condition == "missing_marker":
        marker = ""
    elif condition == "different_marker":
        marker = "WSL_TEST_other_"
    elif condition == "wrong_peer":
        peer = harness.third.nim_account
    elif condition == "missing_peer":
        peer = ""
    elif condition == "membership_peer_changed":
        bot.members[GROUP][SENDER] = harness.third.nim_account
    elif condition == "wrong_scope":
        bot.test_window = harness.Window(
            bot, harness.sender, [GROUP], ["group_ai"], [MARKER], 60, 10
        )
    elif condition == "third_account":
        sender, peer = THIRD, harness.third.nim_account
    elif condition == "epoch_changed":
        bot.config["_business_epoch"] += 1
    elif condition == "receiver_not_registered":
        harness.runtime.open_service.adapters = lambda: [harness.third, harness.sender]
    elif condition == "service_absent":
        harness.runtime.open_service = None

    for n in range(count):
        mid = str(100 + n)
        payload = await ingest(
            harness,
            mid,
            marker + ("repeat" if same else f"item-{n}"),
            group=group,
            sender=sender,
            peer=peer,
        )
        assert "_ingress_flood" not in payload
        assert not harness.test_rules.rule_target(bot, group, sender, mid)
        assert not await handle(harness, mid, payload, group=group)
    assert harness.window.accepted == {}
    if bot.test_window is not None:
        assert bot.test_window.accepted == {}
    assert not bot.settings.flood_windows.windows
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()
    bot.get_moderation_members.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,same", [(5, True), (8, False)], ids=["5-repeats", "8-in-10"]
)
@pytest.mark.parametrize("condition", ["expired", "closed"])
async def test_committed_evidence_cannot_outlive_original_window(
    three_rule_ingress, condition, count, same
):
    harness = three_rule_ingress
    bot = harness.receiver
    for n in range(count):
        mid = str(100 + n)
        payload = await ingest(
            harness, mid, MARKER + ("repeat" if same else f"item-{n}")
        )
    assert payload["_ingress_flood"]["category"] == "message_flood"
    assert len(harness.window.accepted) == count
    if condition == "expired":
        harness.window.expires = 0
    else:
        bot.test_window = None
    assert not harness.test_rules.rule_target(bot, GROUP, SENDER, mid)
    assert not await handle(harness, mid, payload)
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_is_idempotent_and_receiver_is_not_interchangeable(
    three_rule_ingress,
):
    harness = three_rule_ingress
    bot, window = harness.receiver, harness.window
    payload = await ingest(harness, "100", MARKER + "repeat")
    assert harness.test_rules.admit_rule_target(bot, GROUP, "100", payload)
    assert window.remaining == 9
    assert window.accepted == {(GROUP, "100"): "group_rules"}
    assert payload["_ingress_flood"]["count"] == 1

    # A newly constructed, active window still cannot authorize the third receiver.
    harness.third.account, bot.account = RECEIVER, THIRD
    bot.test_window = harness.Window(
        bot, harness.sender, [GROUP], ["group_rules"], [MARKER], 60, 10
    )
    assert bot.test_window.active()
    rejected = await ingest(harness, "101", MARKER + "repeat")
    assert not harness.test_rules.admit_rule_target(bot, GROUP, "101", rejected)
    assert "_ingress_flood" not in rejected
    assert bot.test_window.accepted == {}
    assert not await handle(harness, "101", rejected)
    assert bot.calls == []
    bot.execute_moderation.assert_not_awaited()
    bot.nim.request.assert_not_awaited()
