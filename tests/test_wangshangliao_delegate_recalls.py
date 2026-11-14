"""Delegated mutes recall only bounded, authenticated pre-command messages."""

import asyncio
import json
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from astrbot.builtin_stars.wangshangliao_moderation import message_recalls
from astrbot.builtin_stars.wangshangliao_moderation.message_recalls import (
    MessageRecalls,
)
from astrbot.builtin_stars.wangshangliao_moderation.open_store import OpenError
from astrbot.builtin_stars.wangshangliao_moderation.syntax import recognize_command
from astrbot.core.platform.sources.wangshangliao.actions import dispatch
from astrbot.core.platform.sources.wangshangliao.adapter import WangshangliaoAdapter
from astrbot.core.platform.sources.wangshangliao.storage import Ledger
from tests.test_wangshangliao_command_parity import setup as setup
from tests.test_wangshangliao_delegated_mutes import scope as scope

pytestmark = pytest.mark.usefixtures("wsl_active_policy", "wsl_command_classifier")


def message(mid, received, *, sender="3", peer="903", **changes):
    """Build an exact native inbox row.

    Args:
        mid: Server message ID.
        received: Trusted receive time in seconds.
        sender: Original business UID.
        peer: Original transport identity.
        changes: Additional payload fields.

    Returns:
        Persisted message tuple.
    """
    payload = {
        "sender": sender,
        "kind": "image",
        "recall_route": {
            "client": "client-" + mid,
            "peer": peer,
            "time": str(int(received * 1000)),
        },
        **changes,
    }
    return mid, json.dumps(payload), received


def test_delegated_candidates_include_normal_media_without_flood_proof():
    rows = [
        message("10", 9000),
        message("11", 9001, kind="text"),
        message("12", 9002, kind=None),
    ]
    result = MessageRecalls.delegated_candidates(rows, "3", "903", 8200, 10000)
    assert [row["id"] for row in result] == ["12", "11", "10"]
    assert all(len(row["fingerprint"]) == 64 for row in result)


@pytest.mark.parametrize(
    "row",
    [
        message("10", 8199),
        message("10", 10001),
        message("10", 9000, sender="4"),
        message("10", 9000, peer="904"),
        message("10", 9000, _test_rule_window="old-fixture"),
        message("10", 9000, recall_route={}),
        message("10", 9000, recall_route=None),
        message("10", 9000, recall_route={"client": "c", "peer": "903", "time": "1"}),
        message("bad-id", 9000),
        ("10", "not-json", 9000),
        ("10", "[]", 9000),
    ],
)
def test_delegated_candidates_reject_wrong_identity_time_or_missing_evidence(row):
    assert MessageRecalls.delegated_candidates([row], "3", "903", 8200, 10000) == []


def test_delegated_candidates_include_both_thirty_minute_boundaries():
    rows = [message("10", 8200), message("11", 10000)]
    assert [
        row["id"]
        for row in MessageRecalls.delegated_candidates(rows, "3", "903", 8200, 10000)
    ] == ["11", "10"]


def test_delegated_candidates_reject_oversized_selection_without_truncating():
    rows = [message(str(i + 1), 9000) for i in range(501)]
    with pytest.raises(OpenError, match="recall_selection_limit"):
        MessageRecalls.delegated_candidates(rows, "3", "903", 8200, 10000)


@pytest_asyncio.fixture
async def recall_scope(scope, tmp_path, monkeypatch):
    """Attach a real native ledger to the scoped delegated command fixture.

    Args:
        scope: Existing exact-mention, grant and identity fixture.
        tmp_path: Isolated persistence.
        monkeypatch: Scoped pacing and governance replacements.

    Yields:
        Service, native event, mutable roster and grant file.
    """
    service, event, rows, grant, _ = scope
    adapter = event.platform
    ledger = adapter.ledger = Ledger(tmp_path / "delegate-inbox.sqlite3")
    await ledger.open()
    service.member_evidence[("1", "5")] = rows
    service.member_evidence[("1", "6")] = rows
    adapter.config["moderation"]["automation_enabled"] = True
    adapter.config["moderation"]["permissions"]["5"].append("recall")

    async def governed(lane, receiver, submit, *, group, guard):
        guard()
        return await submit()

    service.governance = SimpleNamespace(run=governed)
    monkeypatch.setattr(message_recalls, "PACE_SECONDS", 0)
    monkeypatch.setattr(message_recalls, "PACE_JITTER_SECONDS", 0)
    service.message_recalls.next_send_at.clear()
    end = event.get_extra("wangshangliao_payload")["created_at"] / 1000
    for mid, received, group, sender in [
        ("10", end - 60, "5", "3"),
        ("11", end - 30, "5", "3"),
        ("12", end - 1801, "5", "3"),
        ("13", end + 1, "5", "3"),
        ("14", end - 10, "6", "3"),
        ("15", end - 10, "5", "4"),
    ]:
        _, raw, _ = message(mid, received, sender=sender, peer="90" + sender)
        await ledger.ingest("1", group, mid, json.loads(raw))
        await ledger.db.execute(
            "UPDATE inbox SET received_at=? WHERE account='1' AND team=? AND message=?",
            (received, group, mid),
        )
    await ledger.db.commit()
    try:
        yield service, event, rows, grant
    finally:
        await service.message_recalls.close()
        await ledger.close()


async def finish_jobs(service):
    """Wait for owned workers without sleeping for production pacing.

    Args:
        service: Isolated moderation service.
    """
    tasks = list(service.message_recalls.tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


@pytest.mark.asyncio
async def test_mute_happens_first_then_only_precommand_thirty_minutes(recall_scope):
    service, event, _, _ = recall_scope
    reply = await service.manager_command(event, recognize_command(event.message_str))
    assert "禁言已受理：128分钟" in reply and "30分钟的2条" in reply
    assert event.platform.execute_action.await_args_list[0].args[0].action == "mute"
    await finish_jobs(service)
    requests = [call.args[0] for call in event.platform.execute_action.await_args_list]
    assert [(r.action, r.message) for r in requests] == [
        ("mute", ""),
        ("recall", "11"),
        ("recall", "10"),
    ]
    assert all(
        r.group == 5 and r.member == 3 and r.expected_nim == "903" for r in requests
    )
    progress = service.message_recalls.snapshot("1", "5")
    assert progress["state"] == "completed"
    assert progress["counts"]["accepted"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "unknown"])
async def test_unaccepted_mute_does_not_recall(recall_scope, status):
    service, event, _, _ = recall_scope
    event.platform.execute_action.side_effect = None
    event.platform.execute_action.return_value = {"status": status}
    await service.manager_command(event, recognize_command(event.message_str))
    await finish_jobs(service)
    assert event.platform.execute_action.await_count == 1
    assert service.message_recalls.snapshot("1", "5") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grant", "promotion", "whitelist", "paused"])
async def test_delegated_recall_rechecks_protection_before_each_message(
    recall_scope, change, monkeypatch
):
    from astrbot.builtin_stars.wangshangliao_moderation import delegated_mutes

    service, event, rows, grant = recall_scope
    submitted = []

    async def execute(request, *, authorize):
        await authorize()
        submitted.append(request.action)
        if request.action == "recall":
            if change == "grant":
                grant.unlink()
            elif change == "promotion":
                rows["3"]["groupRole"] = "GROUP_ROLE_ADMIN"
            elif change == "whitelist":
                monkeypatch.setattr(delegated_mutes, "is_exempt", lambda *args: True)
            else:
                service.settings["paused"] = True
        return {"status": "accepted"}

    event.platform.execute_action.side_effect = execute
    await service.manager_command(event, recognize_command(event.message_str))
    await finish_jobs(service)
    assert submitted == ["mute", "recall"]
    job = service.message_recalls.snapshot("1", "5")
    assert job["state"] == "stopped"
    assert job["counts"]["accepted"] == 1


@pytest.mark.asyncio
async def test_unknown_recall_stops_without_retrying_or_recalling_the_next(
    recall_scope,
):
    service, event, _, _ = recall_scope

    async def execute(request, *, authorize):
        await authorize()
        return {"status": "accepted" if request.action == "mute" else "unknown"}

    event.platform.execute_action.side_effect = execute
    await service.manager_command(event, recognize_command(event.message_str))
    await finish_jobs(service)
    assert event.platform.execute_action.await_count == 2
    job = service.message_recalls.snapshot("1", "5")
    assert job["state"] == "stopped" and job["counts"]["unknown"] == 1
    assert job["remaining"] == 1


@pytest.mark.asyncio
async def test_existing_native_recall_is_skipped(recall_scope):
    service, event, _, _ = recall_scope
    await event.platform.ledger.reserve("recall/1/5/11", "known")
    await event.platform.ledger.finish("recall/1/5/11", "accepted")
    await service.manager_command(event, recognize_command(event.message_str))
    await finish_jobs(service)
    requests = [call.args[0] for call in event.platform.execute_action.await_args_list]
    assert [(r.action, r.message) for r in requests] == [("mute", ""), ("recall", "10")]
    assert service.message_recalls.snapshot("1", "5")["counts"]["skipped"] == 1


@pytest.mark.asyncio
async def test_delegated_job_uses_real_action_dispatch_and_native_recall(recall_scope):
    service, event, _, _ = recall_scope
    adapter = event.platform
    adapter.nim = SimpleNamespace(request=AsyncMock(return_value=(200, b"")))
    adapter.send_lock = asyncio.Lock()
    adapter.diagnostics = SimpleNamespace(emit=Mock())
    adapter.recall_violation = MethodType(
        WangshangliaoAdapter.recall_violation, adapter
    )

    async def execute(request, *, authorize):
        if request.action == "mute":
            await authorize()
            return {"status": "accepted"}
        return await dispatch(adapter, request, authorize=authorize)

    adapter.execute_action.side_effect = execute
    reply = await service.manager_command(event, recognize_command(event.message_str))
    assert "30分钟的2条" in reply
    await finish_jobs(service)
    assert adapter.nim.request.await_count == 2
    assert (await adapter.ledger.receipt("recall/1/5/11"))["status"] == "accepted"
    assert (await adapter.ledger.receipt("recall/1/5/10"))["status"] == "accepted"
    assert service.message_recalls.snapshot("1", "5")["state"] == "completed"


@pytest.mark.asyncio
async def test_same_native_command_does_not_create_or_run_a_second_job(recall_scope):
    service, event, _, _ = recall_scope
    command = recognize_command(event.message_str)
    await service.manager_command(event, command)
    await finish_jobs(service)
    first = service.message_recalls.snapshot("1", "5")
    await service.manager_command(event, command)
    await finish_jobs(service)
    assert service.message_recalls.snapshot("1", "5")["id"] == first["id"]
    requests = [call.args[0] for call in event.platform.execute_action.await_args_list]
    assert sum(request.action == "recall" for request in requests) == 2
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM bulk_recalls").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_invalid_command_time_never_starts_a_job(recall_scope):
    service, event, _, _ = recall_scope
    event.get_extra("wangshangliao_payload")["created_at"] += 100_000_000
    reply = await service.manager_command(event, recognize_command(event.message_str))
    assert "禁言已受理" in reply and "撤回未启动" in reply
    assert service.message_recalls.snapshot("1", "5") is None
    assert event.platform.execute_action.await_count == 1


@pytest.mark.asyncio
async def test_plugin_shutdown_marks_unsubmitted_batch_interrupted(recall_scope):
    service, event, _, _ = recall_scope
    entered, release = asyncio.Event(), asyncio.Event()

    async def governed(lane, receiver, submit, *, group, guard):
        entered.set()
        await release.wait()
        return await submit()

    service.governance.run = governed
    await service.manager_command(event, recognize_command(event.message_str))
    await asyncio.wait_for(entered.wait(), 2)
    await service.message_recalls.close()
    job = service.message_recalls.snapshot("1", "5")
    assert job["state"] == "interrupted"
    assert job["counts"]["unknown"] == 1
    assert event.platform.execute_action.await_count == 1
    MessageRecalls(service)
    assert service.message_recalls.snapshot("1", "5")["state"] == "interrupted"


@pytest.mark.asyncio
async def test_busy_recall_reports_not_started_without_undoing_mute(recall_scope):
    service, event, _, _ = recall_scope
    service.message_recalls.tasks["1"] = asyncio.create_task(asyncio.Event().wait())
    reply = await service.manager_command(event, recognize_command(event.message_str))
    assert "禁言已受理" in reply and "另一批撤回" in reply
    assert service.message_recalls.snapshot("1", "5") is None
    assert event.platform.execute_action.await_count == 1


@pytest.mark.asyncio
async def test_same_message_id_in_another_account_is_not_recalled(recall_scope):
    service, event, _, _ = recall_scope
    ledger = event.platform.ledger
    stamp = event.get_extra("wangshangliao_payload")["created_at"] / 1000 - 5
    _, payload, _ = message("99", stamp)
    await ledger.ingest("2", "5", "99", json.loads(payload))
    await ledger.db.execute(
        "UPDATE inbox SET received_at=? WHERE account='2'", (stamp,)
    )
    await ledger.db.commit()
    await service.manager_command(event, recognize_command(event.message_str))
    await finish_jobs(service)
    assert service.message_recalls.snapshot("1", "5")["total"] == 2
    assert service.message_recalls.snapshot("2", "5") is None


@pytest.mark.asyncio
async def test_bounded_selection_refuses_large_payloads_before_fetching_bodies(
    recall_scope, monkeypatch
):
    service, event, _, _ = recall_scope
    monkeypatch.setattr(message_recalls, "MAX_SELECTION_BYTES", 1)
    reply = await service.manager_command(event, recognize_command(event.message_str))
    assert "禁言已受理" in reply and "安全批量上限" in reply
    assert service.message_recalls.snapshot("1", "5") is None
    assert event.platform.execute_action.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["evidence", "identity", "cancel"])
async def test_recall_wait_revalidates_before_native_submission(recall_scope, change):
    service, event, _, _ = recall_scope
    entered, release = asyncio.Event(), asyncio.Event()
    submitted = []

    async def execute(request, *, authorize):
        await authorize()
        submitted.append(request.action)
        return {"status": "accepted"}

    async def governed(lane, receiver, submit, *, group, guard):
        entered.set()
        await release.wait()
        guard()
        return await submit()

    service.governance.run = governed
    event.platform.execute_action.side_effect = execute
    await service.manager_command(event, recognize_command(event.message_str))
    await asyncio.wait_for(entered.wait(), 2)
    if change == "evidence":
        await event.platform.ledger.db.execute(
            "UPDATE inbox SET payload='{}' WHERE account='1' AND team='5' AND message='11'"
        )
        await event.platform.ledger.db.commit()
    elif change == "identity":
        event.platform.members["5"]["3"] = "new-peer"
    else:
        service.message_recalls.cancelled.add(
            service.message_recalls.snapshot("1", "5")["id"]
        )
    release.set()
    await finish_jobs(service)
    assert submitted == ["mute"]
    assert service.message_recalls.snapshot("1", "5")["counts"]["accepted"] == 0
