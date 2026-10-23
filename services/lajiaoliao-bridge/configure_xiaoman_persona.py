"""Apply a versioned persona only to existing exclusive LajiaoLiao profiles."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

from acceptance_client import ROOT, AcceptanceClient
from optimize_reply_config import plan_profiles

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

PERSONA_ID = "小满｜高拟真生活型"
PRESET = ROOT / "integrations/personas/xiaoman"
TOOLS = ["lajiaoliao_private_management"]


def persona_payload(preset=PRESET):
    """Load the full prompt and paired style examples without real user data.

    Args:
        preset: Versioned source directory containing SOUL.md and dialogs.json.

    Returns:
        Native AstrBot persona payload with an explicit minimal tool allowlist.

    Raises:
        ValueError: If the prompt or alternating dialog structure is malformed.
    """
    prompt = (preset / "SOUL.md").read_text().strip()
    dialogs = json.loads((preset / "dialogs.json").read_text())
    if (
        len(prompt) < 100
        or not isinstance(dialogs, list)
        or not dialogs
        or len(dialogs) % 2
        or any(not isinstance(item, str) or not item.strip() for item in dialogs)
    ):
        raise ValueError("invalid_xiaoman_preset")
    return {
        "persona_id": PERSONA_ID,
        "system_prompt": prompt,
        "begin_dialogs": dialogs,
        "tools": list(TOOLS),
        "skills": [],
    }


def persona_profile(original):
    """Change only the native persona ID in a local AstrBot agent profile.

    Args:
        original: Current exclusive instance profile.

    Returns:
        Independent configuration with model, authorization and switches intact.

    Raises:
        ValueError: If the agent runner is external or missing its persona fields.
    """
    result = copy.deepcopy(original)
    runner = result.get("agent_runner", {})
    if runner.get("runner_type") != "local":
        raise ValueError("local_agent_runner_required")
    if not isinstance(runner.get("config", {}).get("persona"), dict):
        raise ValueError("local_persona_config_required")
    runner["config"]["persona"]["persona_id"] = PERSONA_ID
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and args.backup is None:
        raise ValueError("protected_backup_required")
    client = AcceptanceClient(test_bindings=False)
    config_before = client.original_config
    configured = json.loads(config_before.decode("utf-8-sig"))
    instances = list(dict.fromkeys(args.instance))
    for identifier in instances:
        bot = next(
            (row for row in configured["platform"] if row.get("id") == identifier), {}
        )
        if bot.get("type") != "lajiaoliao" or not bot.get("session_ref"):
            raise ValueError("bound_lajiaoliao_instance_required")
        session = json.loads(
            (
                ROOT
                / "data/plugin_data/astrbot_plugin_lajiaoliao"
                / bot["session_ref"]
                / "session.json"
            ).read_text()
        )
        if (
            session["owner"] != client.owner
            or session["state"] != "bound"
            or session["instance_id"] != identifier
        ):
            raise ValueError("session_owner_or_binding_mismatch")
    routes = client.api("/api/v1/config-routes")["routing"]
    plan = plan_profiles(instances, routes)
    if any(not row["reuse"] for row in plan):
        raise ValueError("exclusive_existing_profile_required")
    metadata = client.api("/api/v1/config-profiles")["info_list"]
    originals = {
        row["id"]: client.api("/api/v1/config-profiles/" + row["id"])["config"]
        for row in metadata
    }
    desired = {
        row["source_profile"]: persona_profile(originals[row["source_profile"]])
        for row in plan
    }
    personas = client.api("/api/v1/personas")
    existing = next((row for row in personas if row["persona_id"] == PERSONA_ID), None)
    payload = persona_payload()
    available_tools = {
        row["name"]
        for row in client.api("/api/v1/tools")
        if row.get("active") is True
    }
    if not set(payload["tools"]).issubset(available_tools):
        raise ValueError("required_persona_tool_unavailable")
    report = {
        "applied": False,
        "persona_id": PERSONA_ID,
        "prompt_chars": len(payload["system_prompt"]),
        "preset_pairs": len(payload["begin_dialogs"]) // 2,
        "tools": payload["tools"],
        "plan": plan,
        "steps": [],
        "dedicated_pc28_tool_available": False,
        "history_or_session_overrides_changed": False,
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    args.backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(args.backup, 0o700)
    atomic_json(
        args.backup / "before.json",
        {
            "personas": personas,
            "profiles": originals,
            "routes": routes,
            "global_config_sha256": hashlib.sha256(config_before).hexdigest(),
        },
    )
    atomic_json(args.backup / "report.json", report)
    if client.api("/api/v1/personas") != personas:
        raise RuntimeError("personas_changed_refresh_required")
    if existing:
        client.api("/api/v1/personas/by-id", payload, method="PUT")
    else:
        client.api("/api/v1/personas", payload, method="POST")
    report["steps"].append({"saved_persona": PERSONA_ID})
    atomic_json(args.backup / "report.json", report)
    for row in plan:
        identifier = row["source_profile"]
        if client.api("/api/v1/config-routes")["routing"] != routes:
            raise RuntimeError("routes_changed_refresh_required")
        if (
            client.api("/api/v1/config-profiles/" + identifier)["config"]
            != originals[identifier]
        ):
            raise RuntimeError("profile_changed_refresh_required")
        client.api(
            "/api/v1/config-profiles/" + identifier,
            desired[identifier],
            method="PUT",
        )
        if (
            client.api("/api/v1/config-profiles/" + identifier)["config"]
            != desired[identifier]
        ):
            raise RuntimeError("profile_verification_failed")
        report["steps"].append({"updated_persona_profile": identifier})
        atomic_json(args.backup / "report.json", report)
    actual = client.api(
        "/api/v1/personas/by-id?persona_id=" + quote(PERSONA_ID, safe="")
    )
    if any(actual[field] != payload[field] for field in payload):
        raise RuntimeError("persona_verification_failed")
    after_personas = client.api("/api/v1/personas")
    if [row for row in after_personas if row["persona_id"] != PERSONA_ID] != [
        row for row in personas if row["persona_id"] != PERSONA_ID
    ]:
        raise RuntimeError("unrelated_personas_changed_review_required")
    for identifier, original in originals.items():
        expected = desired.get(identifier, original)
        if client.api("/api/v1/config-profiles/" + identifier)["config"] != expected:
            raise RuntimeError("unrelated_profile_changed_review_required")
    if client.api("/api/v1/config-routes")["routing"] != routes:
        raise RuntimeError("routes_changed_review_required")
    if (ROOT / "data/cmd_config.json").read_bytes() != config_before:
        raise RuntimeError("global_config_changed_review_required")
    report.update(
        applied=True,
        global_config_unchanged=True,
        unrelated_personas_unchanged=True,
        unrelated_profiles_unchanged=True,
        routes_unchanged=True,
        expected_effect="next_new_model_request",
    )
    atomic_json(args.backup / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
