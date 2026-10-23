"""Scoped two-robot delegation, isolation, quote and removed-action rejection.

No ranking/enrollment exclusions or punitive protections are lifted. Production
grants/routes are restored in finally; an abandoned local lease expires in 5 min.
"""

import argparse
import copy
import hashlib
import json
import secrets
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from acceptance_client import GROUP, PREFIX, ROOT, AcceptanceClient
from acceptance_routes import ScopedTestRoutes

from data.plugins.astrbot_plugin_lajiaoliao.storage import SessionStore, atomic_json
from data.plugins.astrbot_plugin_lajiaoliao.test_window import GroupManagerTestPermit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-accounts", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    client = AcceptanceClient()
    uids = [session["account_id"] for session in client.sessions]
    routes = [
        f"{bot['id']}:FriendMessage:private:{uids[1 - index]}"
        for index, bot in enumerate(client.bots)
    ]
    scopes = [
        {"session_ref": bot["session_ref"], "group_id": GROUP} for bot in client.bots
    ]
    baseline = [client.api(PREFIX + "business", scope) for scope in scopes]
    old_routes = client.api("/api/v1/config-routes")["routing"]
    report = {
        "beijing_time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "mode": "owned_bot_delegation_acceptance_not_human_business_acceptance",
        "group_id": GROUP,
        "steps": [],
        "executed": False,
        "ranking_and_lottery_bot_exclusions_retained": True,
        "native_punishment_protections_retained": True,
        "before_settings": [row["settings"] for row in baseline],
        "before_routes": {route: old_routes.get(route) for route in routes},
    }
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def record():
        atomic_json(args.output, report)

    record()
    if not args.execute:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.confirm_accounts != ",".join(uids):
        raise RuntimeError("explicit_two_test_accounts_required")
    if any(
        row["activities"]
        and any(
            activity["state"] in {"opening", "open", "drawing", "publishing", "unknown"}
            for activity in row["activities"]
        )
        or row["reminders"]
        for row in baseline
    ):
        raise RuntimeError("existing_business_job_in_use")
    for scope in scopes:
        members = client.api(PREFIX + "members", {**scope, "page": 1})["members"]
        if {str(member["userID"]) for member in members} != set(uids):
            raise RuntimeError("test_group_members_changed")
    routing = ScopedTestRoutes(client.api, routes, old_routes)
    before_pid = subprocess.check_output(
        ["systemctl", "show", "astrbot.service", "--property=MainPID", "--value"],
        text=True,
    ).strip()
    store = SessionStore(ROOT / "data/plugin_data/astrbot_plugin_lajiaoliao")
    permit = GroupManagerTestPermit(store)
    windows, permits, profile_id = {}, [], None
    temp_lists = {}
    record()

    def wait_for(label, query, *, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = query()
            if value:
                return value
            time.sleep(0.4)
        raise RuntimeError(label + "_not_confirmed")

    def save_managers(index, users):
        current = client.api(PREFIX + "business", scopes[index])
        return client.api(
            PREFIX + "business",
            {
                **scopes[index],
                "action": "save-managers",
                "confirm": True,
                "manager_user_ids": users,
                "version": current["version"],
                "instance_revision": current["instance_revision"],
            },
        )

    def cursor(index):
        data = json.loads(
            (
                store.directory(client.bots[index]["session_ref"]) / "cursor.json"
            ).read_text()
        )
        return data.get("sequences", {}).get("si_" + "_".join(sorted(uids)), 0)

    def send(index, text, reply_to=""):
        op = "mutual-live-" + secrets.token_hex(8)
        body = {
            "session_ref": client.bots[index]["session_ref"],
            "window_ref": windows[index]["window_ref"],
            "operation_id": op,
            "kind": "private",
            "target_id": uids[1 - index],
            "text": text,
            "reply_to": reply_to,
        }
        entry = {"sender": uids[index], "text": text, "operation_id": op}
        report["steps"].append(entry)
        record()
        receipt = client.api(PREFIX + "test-send", body)
        entry["receipt"] = receipt
        record()
        if receipt.get("status") != "sent":
            raise RuntimeError("message_result_not_sent")
        if client.api(PREFIX + "test-send", body) != receipt:
            raise RuntimeError("duplicate_send_receipt_changed")
        return receipt

    def command(index, text, expected=None):
        receiver = 1 - index
        seen = {
            row["message_id"]
            for row in client.inspect(receiver, "private", uids[index])
        }
        old_cursor = cursor(receiver)
        receipt = send(index, "/" + text)
        wait_for("command_handoff", lambda: cursor(receiver) > old_cursor)
        if expected is None:
            time.sleep(3)
            responses = [
                row
                for row in client.inspect(receiver, "private", uids[index])
                if row["message_id"] not in seen and row["sender_id"] == uids[receiver]
            ]
            if responses:
                raise RuntimeError("revoked_test_bot_command_replied")
            report["steps"][-1]["revoked_command_no_response"] = True
            record()
            return None
        response = wait_for(
            "command_response",
            lambda: next(
                (
                    row
                    for row in client.inspect(receiver, "private", uids[index])
                    if row["message_id"] not in seen
                    and row["sender_id"] == uids[receiver]
                    and expected in row.get("text", "")
                ),
                None,
            ),
        )
        report["steps"][-1]["response"] = {
            key: response[key] for key in ("message_id", "sender_id", "text")
        }
        report["steps"][-1]["input_message_id"] = receipt["message_id"]
        record()
        return response

    try:
        profile = copy.deepcopy(client.api("/api/v1/config-profiles/schema")["config"])
        profile.update(
            admins_id=[],
            plugin_set=["astrbot_plugin_lajiaoliao"],
            dashboard={},
            platform=[],
            provider=[],
            provider_sources=[],
            wake_prefix=["/"],
        )
        profile["provider_settings"]["enable"] = False
        profile["platform_settings"].update(
            enable_id_white_list=False, friend_message_needs_wake_prefix=False
        )
        profile_id = client.api(
            "/api/v1/config-profiles",
            {
                "name": "lajiaoliao-mutual-acceptance-" + secrets.token_hex(8),
                "config": profile,
            },
        )["conf_id"]
        report["temporary_profile"] = profile_id
        record()
        (ROOT / "data/config" / ("abconf_" + profile_id + ".json")).chmod(0o600)
        routing.install(profile_id)
        for index in range(2):
            ref = client.bots[index]["session_ref"]
            permit.create(
                ref,
                client.owner,
                client.bots[1 - index]["session_ref"],
                GROUP,
                confirm=True,
            )
            permits.append(ref)
            previous = baseline[index]["settings"]["manager_user_ids"]
            users = list(dict.fromkeys([*previous, uids[1 - index]]))
            temp_lists[index] = users
            save_managers(index, users)
            windows[index] = client.api(
                PREFIX + "test-window",
                {
                    "session_ref": ref,
                    "action": "open",
                    "account_id": uids[index],
                    "confirm": True,
                    "duration": 300,
                    "budget": 10,
                    "targets": [{"kind": "private", "target_id": uids[1 - index]}],
                },
            )
        report["executed"] = True
        record()
        last_reply = None
        for index in range(2):
            command(index, "群列表", GROUP)
            command(index, "选择群 1", GROUP)
            last_reply = command(index, "我的权限", "本群机器人管理员")
            command(index, "规则", "审核")
        # Revoke only receiver 0: its peer loses control; receiver 1 remains delegated.
        save_managers(0, baseline[0]["settings"]["manager_user_ids"])
        command(1, "规则")
        command(0, "我的权限", "本群机器人管理员")
        save_managers(1, baseline[1]["settings"]["manager_user_ids"])
        command(0, "规则")
        report["mutual_grant_and_revocation"] = "passed"
        # Quote receiver 0's reply; removed Dashboard actions must stay rejected.
        quoted_target = next(
            row
            for row in reversed(client.inspect(1, "private", uids[0]))
            if row["sender_id"] == uids[0] and row.get("text")
        )
        text = "Lajiao private quote acceptance " + secrets.token_hex(4)
        sent = send(1, text, quoted_target["message_id"])
        quote_row = wait_for(
            "private_quote_received",
            lambda: next(
                (
                    row
                    for row in client.inspect(0, "private", uids[1])
                    if row["message_id"] == sent["message_id"]
                    and row.get("quoted_message_id") == quoted_target["message_id"]
                    and row.get("text") == text
                    and row["content_type"] == 114
                ),
                None,
            ),
        )
        report["private_quote"] = {
            key: quote_row[key]
            for key in ("message_id", "quoted_message_id", "content_type", "sender_id")
        }
        management = client.api(PREFIX + "management")["instances"]
        revision = next(
            row["revision"] for row in management if row["id"] == client.bots[1]["id"]
        )
        body = {
            "session_ref": client.bots[1]["session_ref"],
            "instance_id": client.bots[1]["id"],
            "revision": revision,
            "operation_id": "mutual-recall-" + secrets.token_hex(8),
            "action": "revoke_self_message",
            "kind": "private",
            "target_id": uids[0],
            "message_id": sent["message_id"],
            "confirm": True,
        }
        recall = client.api(PREFIX + "platform-action", body, expected=409)
        report["removed_manual_action"] = recall
        record()
        if recall.get("message") != "manual_group_actions_removed":
            raise RuntimeError("removed_manual_action_not_rejected")
        report["private_recall_live"] = "not_retested_manual_dashboard_action_removed"
        report["result"] = "passed_scoped_mutual_authority_quote_action_gate"
        assert last_reply
    except Exception as error:
        report["result"] = "incomplete"
        report["error"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
        record()
    finally:

        def cleanup(label, operation):
            try:
                operation()
            except Exception as error:
                report.setdefault("cleanup_errors", []).append(
                    label + ":" + type(error).__name__
                )
            record()

        for index in temp_lists:

            def restore(index=index):
                now = client.api(PREFIX + "business", scopes[index])["settings"][
                    "manager_user_ids"
                ]
                original = baseline[index]["settings"]["manager_user_ids"]
                if now == temp_lists[index]:
                    save_managers(index, original)
                elif now != original:
                    raise RuntimeError("manager_grant_changed_externally")
                report.setdefault("grants_restored", {})[str(index)] = True

            cleanup("grants-" + str(index), restore)
        for ref in permits:
            cleanup("local_permit", lambda ref=ref: permit.close(ref))
        for index in windows:
            cleanup(
                "test_window",
                lambda index=index: client.api(
                    PREFIX + "test-window",
                    {
                        "session_ref": client.bots[index]["session_ref"],
                        "action": "close",
                    },
                ),
            )
        time.sleep(2)
        if profile_id:

            def restore_routes():
                routing.restore()
                current = client.api("/api/v1/config-routes")["routing"]
                if profile_id not in current.values():
                    client.api("/api/v1/config-profiles/" + profile_id, method="DELETE")
                report["routes_restored"] = list(current.items()) == list(
                    old_routes.items()
                )

            cleanup("routes", restore_routes)
        report["global_config_unchanged"] = (
            hashlib.sha256((ROOT / "data/cmd_config.json").read_bytes()).digest()
            == hashlib.sha256(client.original_config).digest()
        )
        report["main_pid_unchanged"] = (
            before_pid
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
        report["permits_removed"] = all(
            not (store.directory(ref) / "group-manager-test-permit.json").exists()
            for ref in permits
        )
        if (
            report.get("cleanup_errors")
            or not report["permits_removed"]
            or not report["global_config_unchanged"]
        ):
            report["result"] = "incomplete_cleanup"
        record()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["result"] != "passed_scoped_mutual_authority_quote_action_gate":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
