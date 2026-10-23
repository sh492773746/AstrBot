"""One-shot, exact-action acceptance against the explicitly owned test member."""

import argparse
import copy
import json
import secrets
import subprocess
import time
from pathlib import Path

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient

from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json
from data.plugins.astrbot_plugin_lajiaoliao.test_window import NativeTestPermit


def main():
    """Verify member actions, restore reversible changes, and kick only last.

    Returns:
        None. Writes restricted receipts without credentials.

    Raises:
        RuntimeError: Scope, state, receipts, or restoration cannot be confirmed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--kick", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("existing_report_requires_review_not_replay")
    client = AcceptanceClient()
    actor, target = client.bots
    account, member = [row["account_id"] for row in client.sessions]
    if account != "9680209" or member != "3129316":
        raise RuntimeError("explicit_test_binding_changed")
    scope = {"session_ref": actor["session_ref"], "group_id": GROUP}
    store = SessionStore(ROOT / "data/plugin_data/astrbot_plugin_lajiaoliao")
    permit = NativeTestPermit(store)
    permit_owned = False
    temporary = None
    restorations = []
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {
        "started_at": time.time(),
        "actor": actor["id"],
        "actor_uid": account,
        "target": target["id"],
        "target_uid": member,
        "group_id": GROUP,
        "steps": [],
        "cleanup_errors": [],
        "executed": False,
        "passed": False,
        "scope": "native_owned_bot_actions_not_human_business_acceptance",
    }

    def record():
        atomic_json(args.output, report)

    def api(path, body=None):
        return client.api(PREFIX + path, body)

    def state():
        return api("group-state", {**scope, "evidence": True})

    def members():
        result = api("members", {**scope, "page": 1})
        rows = result["members"]
        if len(rows) >= 100:
            raise RuntimeError("incomplete_roster_not_allowed")
        return {row["userID"]: row for row in rows}

    def save(value, current):
        return api(
            "business",
            {
                **scope,
                "action": "save",
                "confirm": True,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
                "settings": value,
            },
        )

    def execute(body, *, duplicate=False):
        entry = {"request": body, "receipt": {"status": "unknown"}}
        report["steps"].append(entry)
        record()
        result = api("platform-action", body)
        entry["receipt"] = result
        record()
        if result.get("status") != "succeeded":
            raise RuntimeError("native_action_" + str(result.get("status", "unknown")))
        query = api(
            "platform-action",
            {
                "session_ref": actor["session_ref"],
                "instance_id": actor["id"],
                "operation_id": body["operation_id"],
                "query": True,
            },
        )
        entry["query_matches"] = query == result
        if duplicate:
            entry["duplicate_matches"] = api("platform-action", body) == result
        record()
        if not entry["query_matches"] or entry.get("duplicate_matches") is False:
            raise RuntimeError("native_receipt_mismatch")
        return entry

    def verify(body):
        action = body["action"]
        if action == "set_member_group_nickname":
            return members().get(member, {}).get("nickname") == body["nickname"]
        if action == "mute":
            return members().get(member, {}).get("muteEndTime", 0) > time.time()
        if action == "unmute":
            return members().get(member, {}).get("muteEndTime") == 0
        if action == "announce":
            return state()["announcement"] == body["announcement"]
        if action == "kick":
            return member not in members()
        return False

    before = api("business", scope)
    original_settings = copy.deepcopy(before["settings"])
    before_state, before_members = state(), members()
    parent = subprocess.check_output(
        ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    report["before"] = {
        "members": before_members,
        "group_status": before_state["group_status"],
        "announcement": before_state["announcement"],
        "settings": original_settings,
        "settings_version": before["version"],
        "main_pid": parent,
    }
    record()
    if not args.execute:
        print(json.dumps({"read_only": True, "members": list(before_members)}))
        return
    if (
        args.confirm_target != member
        or before_state["role_level"] != 100
        or set(before_members) != {account, member}
        or before_members[member].get("roleLevel") != 20
        or before_members[member].get("muteEndTime", 0) != 0
        or before_state["group_status"] != 0
        or any(
            row.get("state") in {"opening", "open", "publishing"}
            for row in before["activities"]
        )
    ):
        raise RuntimeError("unsafe_live_member_precondition")
    try:
        temporary = copy.deepcopy(original_settings)
        temporary["native_actions"] = sorted(
            set(
                temporary["native_actions"]
                + ["mute", "unmute", "announce", "revoke_others", "rename_others"]
                + (["kick"] if args.kick else [])
            )
        )
        current = save(temporary, before)
        report["temporary_grants_version"] = current["version"]
        report["executed"] = True
        record()
        window = api(
            "test-window",
            {
                "session_ref": target["session_ref"],
                "action": "open",
                "confirm": True,
                "account_id": member,
                "duration": 180,
                "budget": 1,
                "targets": [{"kind": "group", "target_id": GROUP}],
            },
        )
        message = api(
            "test-send",
            {
                "session_ref": target["session_ref"],
                "window_ref": window["window_ref"],
                "operation_id": "member-source-" + secrets.token_hex(8),
                "kind": "group",
                "target_id": GROUP,
                "text": "群管验收：本条用于成员撤回测试",
            },
        )
        report["source_receipt"] = message
        record()
        if message.get("status") != "sent":
            raise RuntimeError("source_message_not_confirmed")
        message_id = message["message_id"]
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            found = next(
                (
                    row
                    for row in client.inspect(0, "group", GROUP)
                    if row.get("message_id") == message_id
                ),
                None,
            )
            if found:
                break
            time.sleep(1)
        else:
            raise RuntimeError("source_message_not_visible")
        report["source_native"] = found
        record()

        def body(action, **fields):
            return {
                "session_ref": actor["session_ref"],
                "instance_id": actor["id"],
                "revision": current["instance_revision"],
                "business_version": current["version"],
                "operation_id": "member-" + action + "-" + secrets.token_hex(8),
                "kind": "group",
                "target_id": GROUP,
                "confirm": True,
                "action": action,
                **fields,
            }

        nickname = before_members[member]["nickname"]
        temporary_name = "验收_" + secrets.token_hex(3)
        announcement = before_state["announcement"]
        temporary_notice = "辣椒聊群管验收：临时公告，测试后恢复。"
        revoke = body("revoke_member_message", user_id=member, message_id=message_id)
        rename = body(
            "set_member_group_nickname",
            user_id=member,
            nickname=temporary_name,
            expected_nickname=nickname,
        )
        restore_name = body(
            "set_member_group_nickname",
            user_id=member,
            nickname=nickname,
            expected_nickname=temporary_name,
        )
        mute = body("mute", user_id=member, muted_seconds=60)
        unmute = body("unmute", user_id=member)
        announce = body(
            "announce",
            announcement=temporary_notice,
            expected_announcement=announcement,
        )
        restore_notice = body(
            "announce",
            announcement=announcement,
            expected_announcement=temporary_notice,
        )
        kick = body("kick", user_id=member, confirm_kick=True)
        operations = [
            revoke,
            rename,
            restore_name,
            mute,
            unmute,
            announce,
            restore_notice,
        ]
        if args.kick:
            operations.append(kick)
        permit.create(
            actor["session_ref"],
            client.owner,
            target["session_ref"],
            GROUP,
            operations,
            confirm=True,
        )
        permit_owned = True
        entry = execute(revoke)
        # The durable upstream success receipt is evidence; observation is separate.
        entry["peer_message_after"] = next(
            (
                row
                for row in client.inspect(1, "group", GROUP)
                if row.get("message_id") == message_id
            ),
            None,
        )
        record()
        if (
            entry["peer_message_after"] is None
            or entry["peer_message_after"].get("content_type") != 2101
        ):
            raise RuntimeError("peer_revoke_not_confirmed")
        for change, inverse in (
            (rename, restore_name),
            (mute, unmute),
            (announce, restore_notice),
        ):
            restorations.append(inverse)
            entry = execute(change, duplicate=change["action"] == "mute")
            entry["server_state_confirmed"] = verify(change)
            record()
            if not entry["server_state_confirmed"]:
                raise RuntimeError("native_state_not_confirmed")
            entry = execute(inverse)
            entry["server_state_confirmed"] = verify(inverse)
            record()
            if not entry["server_state_confirmed"]:
                raise RuntimeError("native_restore_not_confirmed")
            restorations.remove(inverse)
        if args.kick:
            entry = execute(kick)
            entry["server_state_confirmed"] = verify(kick)
            report["awaiting_user_reinvite"] = member
            record()
            if not entry["server_state_confirmed"]:
                raise RuntimeError("native_kick_not_confirmed")
        report["native_passed"] = True
    except Exception as error:
        report["failure"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        raise
    finally:
        for inverse in reversed(restorations):
            try:
                # This is the exact authorized inverse, never a retry of an unknown action.
                if not verify(inverse):
                    execute(inverse)
                if not verify(inverse):
                    raise RuntimeError("inverse_state_unknown")
            except Exception:
                report["cleanup_errors"].append("inverse_" + inverse["action"])
        if permit_owned:
            permit.close(actor["session_ref"])
        try:
            api(
                "test-window", {"session_ref": target["session_ref"], "action": "close"}
            )
        except Exception:
            report["cleanup_errors"].append("window_close")
        if temporary is not None:
            try:
                latest = api("business", scope)
                if latest["settings"] != temporary:
                    raise RuntimeError("settings_changed_do_not_overwrite")
                restored = save(original_settings, latest)
                report["settings_restored"] = restored["settings"] == original_settings
                if not report["settings_restored"]:
                    raise RuntimeError("settings_restore_unverified")
            except Exception:
                report["cleanup_errors"].append("settings_restore")
        report["main_pid_unchanged"] = (
            parent
            == subprocess.check_output(
                [
                    "systemctl",
                    "show",
                    "astrbot.service",
                    "--property=MainPID",
                    "--value",
                ],
                text=True,
            ).strip()
        )
        report["core_config_unchanged"] = (
            ROOT / "data/cmd_config.json"
        ).read_bytes() == client.original_config
        report["permit_removed"] = not (
            store.directory(actor["session_ref"]) / "native-test-permit.json"
        ).exists()
        report["passed"] = (
            report.get("native_passed", False)
            and not report["cleanup_errors"]
            and report["main_pid_unchanged"]
            and report["core_config_unchanged"]
            and report["permit_removed"]
            and report.get("settings_restored", False)
        )
        report["completed_at"] = time.time()
        record()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "actions": len(report["steps"]),
                "awaiting_user_reinvite": report.get("awaiting_user_reinvite"),
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
