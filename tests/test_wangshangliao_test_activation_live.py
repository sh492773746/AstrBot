"""Offline, fixed-owner activation transport and restoration regression tests."""

import asyncio
import copy
import hashlib
import json
import socket
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("fcntl", reason="POSIX-only maintenance CLI")

from astrbot.core.platform.sources.wangshangliao import wire
from scripts import check_wsl_test_activation_live as module


@pytest.fixture
def activation(tmp_path, monkeypatch):
    """Create a signed-message fixture without live files, credentials or networking.

    Args:
        tmp_path: Exclusive temporary fixture directory.
        monkeypatch: Scoped patches preventing real IO.

    Returns:
        Check and controlled state with a verified owner invitation.
    """
    monkeypatch.setattr(
        socket.socket, "connect", Mock(side_effect=AssertionError("No live network"))
    )
    monkeypatch.setattr(module, "ROOT", tmp_path / "unused-live-root")
    clock = [time.time()]
    sleep = asyncio.sleep

    async def advance(seconds):
        clock[0] += seconds
        await sleep(0)

    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.asyncio, "sleep", advance)
    state = {
        "config": {
            "wangshangliao_open": {"mode": "integrated"},
            "platform": [
                {
                    "id": module.SENDER,
                    "type": "wangshangliao",
                    "account_id": module.OWNER,
                    "enable": True,
                },
                {
                    "id": module.RECEIVER,
                    "type": "wangshangliao",
                    "account_id": module.PEER,
                    "enable": True,
                },
            ],
        },
        "config_sha256": "original-config",
        "protected": "finance-and-formal-unchanged",
        "owner": {
            "account": module.OWNER,
            "active": 0,
            "present": 1,
            "team": "64031167010",
        },
        "candidate": {
            "account": module.PEER,
            "group_id": module.GROUP,
            "epoch": 1,
            "version": 1,
            "active": 0,
            "present": 1,
            "paused": 0,
            "managers": "[]",
            "admission": "pending",
            "inviter": "",
            "auto_claim": 1,
            "joined": clock[0] - 50,
            "team": "64031167010",
            "native_role": "GROUP_ROLE_MEMBER",
            "cleanup_epoch": 0,
            "cleanup_done": 0,
        },
        "reset_at": clock[0] - 60,
        "reset_confirmation": "reset-digest",
        "departure_fence": None,
    }
    pins = {module.OWNER: "1001", module.PEER: "1002", module.ORDINARY: "1003"}
    roster = [
        {
            "userId": uid,
            "nimId": pin,
            "groupRole": "GROUP_ROLE_OWNER"
            if uid == module.OWNER
            else "GROUP_ROLE_MEMBER",
            "accountState": "ACCOUNT_STATE_GOOD",
        }
        for uid, pin in pins.items()
    ]
    proof = {
        "groupId": module.GROUP,
        "applyId": "88001",
        "applyTime": datetime.fromtimestamp(clock[0] - 40, timezone.utc).isoformat(),
        "groupApplyType": "INVITATION",
        "state": "MEMBER_STATE_GOOD",
        "applicant": {"userId": module.PEER, "nimId": pins[module.PEER]},
        "inviter": {"userId": module.OWNER},
    }
    events = []
    api_error, native_error = [None], [None]

    def response(value):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"status": "ok", "data": value},
        )

    async def api(method, path, json=None):
        events.append(("api", method, path, json))
        if method == "PATCH":
            state["config"]["platform"][0]["enable"] = json["enabled"]
            if api_error[0]:
                error, api_error[0] = api_error[0], None
                raise error
            return response(None)
        return response(
            {
                "platforms": [
                    {
                        "id": module.SENDER,
                        "status": "running"
                        if state["config"]["platform"][0]["enable"]
                        else "stopped",
                        "connection_state": "online"
                        if state["config"]["platform"][0]["enable"]
                        else "stopped",
                    },
                    {
                        "id": module.RECEIVER,
                        "status": "running",
                        "connection_state": "online",
                    },
                    {
                        "id": "telegram_fixture",
                        "status": "running",
                        "connection_state": None,
                    },
                ]
            }
        )

    async def business(route, params, authorize):
        authorize()
        events.append(("business", route, copy.deepcopy(params)))
        if route.endswith("get-group-members"):
            return {"groupMemberInfo": copy.deepcopy(roster)}
        return {"list": [copy.deepcopy(proof)]}

    async def connect(deployment, peer, token):
        assert not state["config"]["platform"][0]["enable"]
        assert peer == pins[module.OWNER] and token == "fixture-nim-session"
        nim.closed.clear()
        nim.socket.closed = False
        events.append(("nim_connect", peer))

    async def close():
        nim.closed.set()
        nim.socket.closed = True
        events.append(("nim_close",))

    async def send(service, command, payload):
        assert (service, command) == (8, 2)
        assert not state["config"]["platform"][0]["enable"]
        fields, end = wire.read_properties(payload)
        assert end == len(payload)
        assert fields[1] == b"64031167010" and fields[8] == b"100"
        inner = wire.open_message(b"x" * 32, fields[10].decode())
        assert str(inner.sender.id) == module.OWNER
        assert str(inner.target.id) == module.GROUP
        assert inner.content.data == module.TEXT and inner.session == 2
        events.append(("nim_send", inner.sender.id, inner.content.data))
        if native_error[0]:
            raise native_error[0]
        state["candidate"].update(
            active=1,
            managers=json.dumps([module.OWNER]),
            inviter=module.OWNER,
            admission="verified",
            version=2,
        )
        check.replies.append(
            "\u672c\u7fa4\u5df2\u5f00\u901a\uff01\u8bf7\u5148\u628a\u673a\u5668\u4eba\u8bbe\u4e3a\u7fa4\u7ba1\u7406\u5458\u3002"
        )
        return 200, wire.properties(
            [(12, b"12345"), (7, b"1792200000000"), (11, fields[11])]
        )

    journal = sqlite3.connect(tmp_path / "journal.sqlite3")
    journal.execute(
        "CREATE TABLE attempts(id TEXT PRIMARY KEY,epoch INTEGER,send_state TEXT,restore_state TEXT,created REAL)"
    )
    pacing = sqlite3.connect(tmp_path / "pacing.sqlite3")
    pacing.executescript(
        "CREATE TABLE pacing(id INTEGER PRIMARY KEY,request_finished REAL,write_finished REAL,blocked_until REAL);"
        "INSERT INTO pacing VALUES(1,0,0,0)"
    )
    nim = SimpleNamespace(
        connect=AsyncMock(side_effect=connect),
        close=AsyncMock(side_effect=close),
        request=AsyncMock(side_effect=send),
        heartbeat=AsyncMock(),
        acknowledge=AsyncMock(),
        pushes=asyncio.Queue(),
        closed=asyncio.Event(),
        socket=SimpleNamespace(closed=True),
        reader=None,
    )
    outbox = {}

    async def reserve(key, fingerprint):
        if key in outbox:
            return "123", "unknown"
        outbox[key] = fingerprint
        return "123", "new"

    ledger = SimpleNamespace(
        open=AsyncMock(),
        close=AsyncMock(),
        reserve=AsyncMock(side_effect=reserve),
        finish=AsyncMock(),
    )
    check = module.ActivationCheck(
        SimpleNamespace(request=AsyncMock(side_effect=api)),
        SimpleNamespace(
            fields=[None, None, None, module.PEER],
            request=AsyncMock(side_effect=business),
        ),
        SimpleNamespace(key=lambda _: b"x" * 32),
        {
            "business": {"uid": module.OWNER},
            "nim_id": pins[module.OWNER],
            "nim_token": "fixture-nim-session",
        },
        pins,
        nim,
        ledger,
        journal,
        tmp_path,
    )
    check.pacing = pacing
    monkeypatch.setattr(check, "local", lambda: copy.deepcopy(state))
    monkeypatch.setattr(check, "owner_closed", lambda: check.disable_confirmed)

    async def waiting():
        await asyncio.Event().wait()

    monkeypatch.setattr(check, "witness", waiting)
    monkeypatch.setattr(check, "heartbeat", waiting)
    try:
        yield SimpleNamespace(
            check=check,
            state=state,
            events=events,
            roster=roster,
            proof=proof,
            pins=pins,
            journal=journal,
            pacing=pacing,
            clock=clock,
            api_error=api_error,
            native_error=native_error,
            nim=nim,
        )
    finally:
        journal.close()
        pacing.close()


@pytest.mark.asyncio
async def test_real_owner_payload_one_send_close_before_restore_and_no_authority_bypass(
    activation,
):
    lab = activation
    result = await lab.check.run()
    assert result["sent"] and result["activation_verified"] and result["restored"]
    assert result["genuinely_new_group"] is False
    assert result["requires_native_admin"] is True
    assert lab.nim.connect.await_count == lab.nim.request.await_count == 1
    close_index = lab.events.index(("nim_close",))
    restore_index = next(
        i
        for i, event in enumerate(lab.events)
        if event[:3] == ("api", "PATCH", module.ENABLE) and event[3]["enabled"]
    )
    assert close_index < restore_index
    assert all(
        event[2] == module.ENABLE
        for event in lab.events
        if event[:2] == ("api", "PATCH")
    )
    assert lab.state["owner"]["active"] == 0
    assert lab.journal.execute(
        "SELECT send_state,restore_state FROM attempts"
    ).fetchone() == ("accepted", "verified")
    assert lab.pacing.execute("SELECT write_finished FROM pacing").fetchone()[0] > 0


@pytest.mark.parametrize(
    "bad",
    [
        "wrong_inviter",
        "stale",
        "natural",
        "wrong_peer",
        "rejected",
        "extra_member",
        "role",
        "vault",
        "formal",
    ],
)
@pytest.mark.asyncio
async def test_preflight_refuses_untrusted_or_changed_context_before_disabling(
    activation, bad
):
    lab = activation
    if bad == "wrong_inviter":
        lab.proof["inviter"]["userId"] = module.ORDINARY
    elif bad == "stale":
        lab.proof["applyTime"] = datetime.fromtimestamp(
            lab.state["reset_at"] - 1, timezone.utc
        ).isoformat()
    elif bad == "natural":
        lab.proof["groupApplyType"] = "APPLY"
    elif bad == "wrong_peer":
        lab.proof["applicant"]["nimId"] = "999"
    elif bad == "rejected":
        lab.proof["state"] = "MEMBER_STATE_REJECTED"
    elif bad == "extra_member":
        lab.roster.append(
            {"userId": "999", "nimId": "999", "groupRole": "GROUP_ROLE_MEMBER"}
        )
    elif bad == "role":
        lab.roster[0]["groupRole"] = "GROUP_ROLE_MEMBER"
    elif bad == "vault":
        lab.check.saved["business"]["uid"] = module.ORDINARY
    elif bad == "formal":
        lab.state["candidate"]["group_id"] = "1516387"
        lab.state["candidate"]["active"] = 1
    with pytest.raises(RuntimeError):
        await lab.check.run()
    assert not any(event[:2] == ("api", "PATCH") for event in lab.events)
    lab.nim.connect.assert_not_awaited()
    lab.nim.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_nim_result_restores_owner_but_never_resends(activation):
    lab = activation
    lab.native_error[0] = TimeoutError("fixture")
    with pytest.raises(TimeoutError):
        await lab.check.run()
    assert lab.check.result["restored"]
    assert (
        lab.journal.execute("SELECT send_state FROM attempts").fetchone()[0]
        == "unknown"
    )
    with pytest.raises(RuntimeError, match="prior_activation_attempt"):
        await lab.check.run()
    assert lab.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_unknown_disable_still_restores_without_opening_second_nim(activation):
    lab = activation
    lab.api_error[0] = TimeoutError("fixture")
    with pytest.raises(TimeoutError):
        await lab.check.run()
    assert lab.check.result["restored"]
    lab.nim.connect.assert_not_awaited()
    lab.nim.request.assert_not_awaited()
    assert lab.state["config"]["platform"][0]["enable"] is True


@pytest.mark.asyncio
async def test_close_failure_does_not_start_competing_owner_adapter(activation):
    lab = activation
    lab.nim.close.side_effect = TimeoutError("fixture")
    with pytest.raises(TimeoutError):
        await lab.check.run()
    assert lab.state["config"]["platform"][0]["enable"] is False
    assert not any(
        event[:3] == ("api", "PATCH", module.ENABLE) and event[3]["enabled"]
        for event in lab.events
    )


@pytest.mark.asyncio
async def test_changed_configuration_is_not_overwritten_on_restore(activation):
    lab = activation
    original = lab.nim.request.side_effect

    async def mutate(*args):
        value = await original(*args)
        lab.state["config"]["operator_change"] = True
        return value

    lab.nim.request.side_effect = mutate
    with pytest.raises(RuntimeError, match="manual_restore"):
        await lab.check.run()
    assert lab.state["config"]["operator_change"]
    assert lab.state["config"]["platform"][0]["enable"] is False
    assert not lab.check.result["restored"]


@pytest.mark.asyncio
async def test_api_whitelist_has_no_general_proxy(activation):
    check = activation.check
    for method, path, body in (
        ("PATCH", f"/api/v1/bots/{module.RECEIVER}/enabled", {"enabled": False}),
        ("POST", "/api/v1/im/messages", {}),
        ("PATCH", module.ENABLE, {"enabled": True, "account": module.OWNER}),
        ("PATCH", module.ENABLE, {"enabled": 1}),
        ("GET", "https://example.invalid", None),
    ):
        with pytest.raises(RuntimeError, match="outside_activation_scope"):
            await check.api(method, path, body)
    check.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_does_not_open_credentials_files_or_network(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["check_wsl_test_activation_live"])
    monkeypatch.setattr(
        module,
        "CONTROL",
        SimpleNamespace(mkdir=Mock(side_effect=AssertionError("No files"))),
    )
    await module.main()
    value = json.loads(capsys.readouterr().out)
    assert value["dry_run"] and value["message"] == module.TEXT
    assert value["owner"] == module.OWNER
    assert value["genuinely_new_group"] is False


@pytest.mark.asyncio
async def test_shared_unknown_cooldown_blocks_before_native_connect(activation):
    lab = activation
    lab.pacing.execute("UPDATE pacing SET blocked_until=?", (lab.clock[0] + 100,))
    lab.pacing.commit()
    with pytest.raises(RuntimeError, match="cooldown"):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()
    assert not any(event[:2] == ("api", "PATCH") for event in lab.events)


@pytest.mark.asyncio
async def test_stats_removal_without_post_close_evidence_never_connects(
    activation, monkeypatch
):
    lab = activation
    monkeypatch.setattr(lab.check, "owner_closed", lambda: False)
    with pytest.raises(RuntimeError, match="transition_not_verified"):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()
    assert lab.check.result["restored"]


@pytest.mark.asyncio
async def test_background_display_metadata_does_not_abort_activation(activation):
    lab = activation
    original = lab.check.local
    sequence = [0]

    def refreshed():
        value = original()
        sequence[0] += 1
        for name in ("candidate", "owner"):
            value[name]["name"] = f"display-{sequence[0]}"
            value[name]["checked"] = sequence[0]
        return value

    lab.check.local = refreshed
    result = await lab.check.run()
    assert result["activation_verified"] and result["restored"]


@pytest.mark.parametrize(
    "field", ["version", "epoch", "present", "managers", "rules", "team"]
)
@pytest.mark.asyncio
async def test_authority_changes_still_abort_before_sending(activation, field):
    lab = activation
    original = lab.nim.connect.side_effect

    async def changed(*args):
        await original(*args)
        lab.state["candidate"][field] = "changed"

    lab.nim.connect.side_effect = changed
    with pytest.raises(RuntimeError, match="version_or_membership"):
        await lab.check.run()
    lab.nim.request.assert_not_awaited()
    assert lab.check.result["restored"]


def test_post_close_proof_requires_ack_and_new_exact_instance_log(
    activation, monkeypatch
):
    check = activation.check
    check.owner_closed = module.ActivationCheck.owner_closed.__get__(check)
    check.disable_started = activation.clock[0]
    marker = f"Wangshangliao instance={module.SENDER} stage=connection outcome=stopped "
    rows = [
        {"__REALTIME_TIMESTAMP": int(check.disable_started * 1e6), "MESSAGE": marker}
    ]
    reader = Mock(return_value=SimpleNamespace(stdout=json.dumps(rows[0])))
    monkeypatch.setattr(module.subprocess, "run", reader)
    assert not check.owner_closed()
    reader.assert_not_called()
    check.disable_confirmed = True
    assert check.owner_closed()
    rows[0]["__REALTIME_TIMESTAMP"] -= 1000000
    reader.return_value.stdout = json.dumps(rows[0])
    assert not check.owner_closed()
    rows[0]["__REALTIME_TIMESTAMP"] += 1000000
    rows[0]["MESSAGE"] = marker.replace(module.SENDER, module.RECEIVER)
    reader.return_value.stdout = json.dumps(rows[0])
    assert not check.owner_closed()


@pytest.mark.parametrize("encoding", ["text", "byte_array", "ansi_byte_array"])
def test_post_close_log_decodes_systemd_binary_message(
    activation, monkeypatch, encoding
):
    check = activation.check
    check.owner_closed = module.ActivationCheck.owner_closed.__get__(check)
    check.disable_confirmed = True
    check.disable_started = activation.clock[0]
    message = (
        f"Wangshangliao instance={module.SENDER} stage=connection outcome=stopped "
        "error=none"
    )
    if encoding == "ansi_byte_array":
        message = "\x1b[32m[Core]\x1b[0m " + message.replace(
            "stage=connection", "\x1b[1mstage=connection\x1b[0m"
        )
    if encoding != "text":
        message = list(message.encode())
    row = {
        "__REALTIME_TIMESTAMP": int(check.disable_started * 1e6),
        "MESSAGE": message,
    }
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=json.dumps(row))),
    )
    assert check.owner_closed()


@pytest.mark.parametrize(
    "message", [[True], [-1], [256], ["stopped"], [255], {"MESSAGE": "stopped"}, None]
)
def test_malformed_binary_log_never_proves_shutdown(activation, monkeypatch, message):
    check = activation.check
    check.owner_closed = module.ActivationCheck.owner_closed.__get__(check)
    check.disable_confirmed = True
    check.disable_started = activation.clock[0]
    row = {
        "__REALTIME_TIMESTAMP": int(check.disable_started * 1e6),
        "MESSAGE": message,
    }
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout=json.dumps(row))),
    )
    assert not check.owner_closed()


@pytest.fixture
def retry_activation(activation, tmp_path, monkeypatch):
    """Preserve one original, proven-unsent private receipt and empty outbox."""
    lab = activation
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    previous = control / ("run-" + "1" * 32)
    previous.mkdir(mode=0o700)
    output = control / ("run-" + "2" * 32)
    output.mkdir(mode=0o700)
    monkeypatch.setattr(module, "CONTROL", control)
    lab.check.output = output
    operation = hashlib.sha256(
        json.dumps(
            [
                module.PEER,
                module.GROUP,
                lab.state["candidate"]["epoch"],
                lab.proof["applyId"],
                lab.state["reset_confirmation"],
            ]
        ).encode()
    ).hexdigest()
    lab.journal.execute(
        "INSERT INTO attempts VALUES(?,1,'not_sent','verified',?)",
        (operation, lab.clock[0] - 60),
    )
    lab.journal.commit()
    evidence = {
        "operation": operation,
        "owner": module.OWNER,
        "bot": module.PEER,
        "group": module.GROUP,
        "epoch": 1,
        "invitation_id": lab.proof["applyId"],
        "reset_existing_group": True,
        "genuinely_new_group": False,
        "sent": False,
        "restored": True,
        "activation_verified": False,
        "state": "incomplete",
    }
    path = previous / "evidence.json"
    path.write_text(json.dumps(evidence))
    path.chmod(0o600)
    with sqlite3.connect(previous / "sender.sqlite3") as db:
        db.execute(
            "CREATE TABLE outbox(key TEXT PRIMARY KEY,fingerprint TEXT,state TEXT)"
        )
    lab.check.retry_not_sent = operation
    lab.previous = previous
    lab.previous_evidence = evidence
    lab.parent = operation
    return lab


@pytest.mark.asyncio
async def test_explicit_unsent_recovery_preserves_parent_and_links_new_attempt(
    retry_activation,
):
    lab = retry_activation
    before = lab.journal.execute("SELECT * FROM attempts").fetchone()
    receipt = (lab.previous / "evidence.json").read_bytes()
    result = await lab.check.run()
    assert result["activation_verified"] and result["restored"]
    assert result["retry_of"] == lab.parent
    assert result["attempt"] != lab.parent
    assert (
        lab.journal.execute(
            "SELECT * FROM attempts WHERE id=?", (lab.parent,)
        ).fetchone()
        == before
    )
    assert lab.journal.execute(
        "SELECT send_state,restore_state FROM attempts WHERE id=?", (result["attempt"],)
    ).fetchone() == ("accepted", "verified")
    assert lab.journal.execute(
        "SELECT parent,child FROM attempt_recoveries"
    ).fetchall() == [(lab.parent, result["attempt"])]
    assert (lab.previous / "evidence.json").read_bytes() == receipt
    assert lab.nim.request.await_count == 1


@pytest.mark.parametrize("state", ["unknown", "accepted", "rejected", "sending"])
@pytest.mark.asyncio
async def test_recovery_never_replays_submitted_or_unknown_attempts(
    retry_activation, state
):
    lab = retry_activation
    lab.journal.execute("UPDATE attempts SET send_state=?", (state,))
    lab.journal.commit()
    with pytest.raises(RuntimeError, match="prior_activation_attempt"):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()
    assert not any(event[:2] == ("api", "PATCH") for event in lab.events)


@pytest.mark.parametrize(
    "bad",
    [
        "no_opt_in",
        "wrong_attempt",
        "restore_unknown",
        "outbox_nonempty",
        "receipt_sent",
        "receipt_restore",
        "receipt_identity",
        "receipt_invitation",
        "duplicate_receipt",
        "missing_outbox",
        "epoch_changed",
    ],
)
@pytest.mark.asyncio
async def test_unsent_recovery_requires_complete_matching_evidence(
    retry_activation, bad
):
    lab = retry_activation
    if bad == "no_opt_in":
        lab.check.retry_not_sent = None
    elif bad == "wrong_attempt":
        lab.check.retry_not_sent = "f" * 64
    elif bad == "restore_unknown":
        lab.journal.execute("UPDATE attempts SET restore_state='unknown'")
        lab.journal.commit()
    elif bad == "outbox_nonempty":
        with sqlite3.connect(lab.previous / "sender.sqlite3") as db:
            db.execute("INSERT INTO outbox VALUES('intent','fingerprint','unknown')")
    elif bad == "missing_outbox":
        (lab.previous / "sender.sqlite3").unlink()
    elif bad == "epoch_changed":
        lab.journal.execute("UPDATE attempts SET epoch=2")
        lab.journal.commit()
    elif bad == "duplicate_receipt":
        other = module.CONTROL / ("run-" + "3" * 32)
        other.mkdir(mode=0o700)
        (other / "evidence.json").write_text(json.dumps(lab.previous_evidence))
    else:
        key, value = {
            "receipt_sent": ("sent", True),
            "receipt_restore": ("restored", False),
            "receipt_identity": ("owner", module.ORDINARY),
            "receipt_invitation": ("invitation_id", "old"),
        }[bad]
        lab.previous_evidence[key] = value
        (lab.previous / "evidence.json").write_text(json.dumps(lab.previous_evidence))
    with pytest.raises(RuntimeError):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()
    lab.nim.request.assert_not_awaited()
    assert not any(event[:2] == ("api", "PATCH") for event in lab.events)


@pytest.mark.asyncio
async def test_unknown_recovery_keeps_parent_and_child_without_next_retry(
    retry_activation,
):
    lab = retry_activation
    lab.native_error[0] = TimeoutError("fixture")
    with pytest.raises(TimeoutError):
        await lab.check.run()
    rows = lab.journal.execute(
        "SELECT id,send_state,restore_state FROM attempts ORDER BY created"
    ).fetchall()
    assert rows[0] == (lab.parent, "not_sent", "verified")
    assert rows[1][1:] == ("unknown", "verified")
    lab.check.retry_not_sent = rows[1][0]
    with pytest.raises(RuntimeError, match="prior_activation_attempt"):
        await lab.check.run()
    assert lab.nim.request.await_count == 1


@pytest.mark.asyncio
async def test_recovery_rechecks_original_outbox_before_durable_attempt_insert(
    retry_activation,
):
    lab = retry_activation
    original = lab.check.record

    def changed(state, **values):
        original(state, **values)
        if state == "preflight_verified":
            with sqlite3.connect(lab.previous / "sender.sqlite3") as db:
                db.execute("INSERT INTO outbox VALUES('late','fingerprint','unknown')")

    lab.check.record = changed
    with pytest.raises(RuntimeError, match="outbox_nonempty"):
        await lab.check.run()
    assert lab.journal.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    lab.nim.connect.assert_not_awaited()
    assert not any(event[:2] == ("api", "PATCH") for event in lab.events)


@pytest.mark.asyncio
async def test_recovery_cli_dry_run_needs_no_files_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["check_wsl_test_activation_live", "--retry-not-sent", "a" * 64]
    )
    monkeypatch.setattr(
        module,
        "CONTROL",
        SimpleNamespace(mkdir=Mock(side_effect=AssertionError("No files"))),
    )
    await module.main()
    value = json.loads(capsys.readouterr().out)
    assert value["dry_run"] and value["retry_not_sent"] == "a" * 64


@pytest.mark.parametrize("admission", ["pending", "verified"])
@pytest.mark.parametrize("has_fence", [True, False])
@pytest.mark.asyncio
async def test_delayed_first_observation_requires_original_verified_departure(
    activation, has_fence, admission
):
    lab = activation
    now = lab.clock[0]
    lab.state["reset_at"] = now - 400
    lab.state["candidate"]["joined"] = now - 1
    lab.state["candidate"]["admission"] = admission
    if admission == "verified":
        lab.state["candidate"].update(
            inviter=module.OWNER, evidence=lab.proof["applyId"], version=2
        )
    lab.state["departure_fence"] = now - 500 if has_fence else None
    lab.proof["applyTime"] = datetime.fromtimestamp(now - 233, timezone.utc).isoformat()
    if has_fence:
        result = await lab.check.run()
        assert result["activation_verified"] and result["restored"]
    else:
        with pytest.raises(RuntimeError, match="fresh_successful"):
            await lab.check.run()
        lab.nim.connect.assert_not_awaited()
        assert not any(event[:2] == ("api", "PATCH") for event in lab.events)


@pytest.mark.parametrize("age", [0, 1])
@pytest.mark.asyncio
async def test_invitation_at_or_before_generation_fence_is_refused(activation, age):
    lab = activation
    lab.state["departure_fence"] = int(lab.clock[0]) - 40
    lab.proof["applyTime"] = datetime.fromtimestamp(
        lab.state["departure_fence"] - age, timezone.utc
    ).isoformat()
    with pytest.raises(RuntimeError, match="fresh_successful"):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()


@pytest.mark.parametrize("age", [120, 121])
@pytest.mark.asyncio
async def test_no_departure_uses_production_joined_minus_120_fallback(activation, age):
    lab = activation
    lab.state["reset_at"] = lab.clock[0] - 300
    lab.state["candidate"]["joined"] = int(lab.clock[0]) - 50
    lab.proof["applyTime"] = datetime.fromtimestamp(
        lab.state["candidate"]["joined"] - age, timezone.utc
    ).isoformat()
    if age == 120:
        assert (await lab.check.run())["activation_verified"]
    else:
        with pytest.raises(RuntimeError, match="fresh_successful"):
            await lab.check.run()
        lab.nim.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_departure_does_not_override_post_reset_requirement(activation):
    lab = activation
    lab.state["departure_fence"] = lab.clock[0] - 500
    lab.proof["applyTime"] = datetime.fromtimestamp(
        lab.state["reset_at"] - 1, timezone.utc
    ).isoformat()
    with pytest.raises(RuntimeError, match="fresh_successful"):
        await lab.check.run()
    lab.nim.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_verified_departure_stops_unsent_activation(activation):
    lab = activation
    lab.state["departure_fence"] = lab.clock[0] - 100
    original = lab.nim.connect.side_effect

    async def changed(*args):
        await original(*args)
        lab.state["departure_fence"] = lab.clock[0] - 10

    lab.nim.connect.side_effect = changed
    with pytest.raises(RuntimeError, match="protected_state"):
        await lab.check.run()
    lab.nim.request.assert_not_awaited()
    assert lab.check.result["restored"]


def test_local_fence_uses_exact_production_generation_predicate(
    activation, tmp_path, monkeypatch
):
    """Exclude unverified, other-account/team and later-generation departures."""
    root = module.ROOT
    paths = root / "data/plugin_data"
    paths.mkdir(parents=True)
    (root / "data/cmd_config.json").write_text(json.dumps(activation.state["config"]))
    finance = paths / "wangshangliao_cashback/cashback.sqlite3"
    finance.parent.mkdir()
    with sqlite3.connect(finance) as db:
        db.execute(
            "CREATE TABLE entries("
            + ",".join(
                f'"{field}" TEXT' for field in module.FINANCIAL_FIELDS.split(",")
            )
            + ")"
        )
        db.execute("CREATE TABLE groups(id TEXT)")
    open_path = paths / "wangshangliao_open/open-service.sqlite3"
    open_path.parent.mkdir()
    with sqlite3.connect(open_path) as db:
        db.executescript(
            "CREATE TABLE tenants(account TEXT,group_id TEXT,team TEXT,joined REAL,paused INTEGER);"
            "CREATE TABLE membership_notices(account TEXT,team TEXT,message TEXT,kind TEXT,event_at REAL,state TEXT);"
            "CREATE TABLE test_reset_audit(id INTEGER PRIMARY KEY,account TEXT,group_id TEXT,created REAL,confirmation TEXT);"
        )
        for account in (module.OWNER, module.PEER):
            db.execute(
                "INSERT INTO tenants VALUES(?,?,?,?,0)",
                (account, module.GROUP, "64031167010", 200),
            )
            for group in ("1516387", "1488006"):
                db.execute(
                    "INSERT INTO tenants VALUES(?,?,?,?,1)",
                    (account, group, "formal-team", 50),
                )
        db.executemany(
            "INSERT INTO membership_notices VALUES(?,?,?,?,?,?)",
            [
                (module.PEER, "64031167010", "old", "leave", 100, "departure_verified"),
                (
                    module.PEER,
                    "64031167010",
                    "latest",
                    "kick",
                    150,
                    "departure_verified",
                ),
                (
                    module.PEER,
                    "64031167010",
                    "later",
                    "kick",
                    201,
                    "departure_verified",
                ),
                (module.PEER, "64031167010", "pending", "kick", 199, "pending"),
                (
                    module.PEER,
                    "64031167010",
                    "role",
                    "role_lost",
                    199,
                    "departure_verified",
                ),
                (
                    module.OWNER,
                    "64031167010",
                    "owner",
                    "kick",
                    199,
                    "departure_verified",
                ),
                (module.PEER, "other-team", "other", "kick", 199, "departure_verified"),
            ],
        )
        db.execute(
            "INSERT INTO test_reset_audit VALUES(1,?,?,120,'original')",
            (module.PEER, module.GROUP),
        )
    receipt = tmp_path / "completion.json"
    receipt.write_text(
        json.dumps(
            {
                "status": "reset_complete",
                "account": module.PEER,
                "manager": module.OWNER,
                "group": module.GROUP,
                "reset_existing_group": True,
                "genuinely_new_group": False,
                "confirmation_sha256": "original",
            }
        )
    )
    monkeypatch.setattr(module, "RESET_RECEIPT", receipt)
    result = module.ActivationCheck.local(activation.check)
    assert result["departure_fence"] == 150
