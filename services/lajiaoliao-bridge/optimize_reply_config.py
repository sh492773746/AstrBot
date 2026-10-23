"""Audit or apply instance-scoped LajiaoLiao reply profiles via Dashboard APIs.

Read-only by default. Never changes login sessions, global configuration,
conversation history, or routes belonging to another platform.
"""

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import jwt

from data.plugins.astrbot_plugin_lajiaoliao.storage import atomic_json

SYSTEM_KEYS = ("dashboard", "platform", "provider", "provider_sources")
NATIVE_TYPE = "googlegenai_chat_completion"


def reply_profile(original, provider_id, max_turns=20):
    """Keep permissions/persona, but isolate model and supported reply settings."""
    profile = copy.deepcopy(original)
    for key in SYSTEM_KEYS:
        profile.pop(key, None)
    if profile.get("agent_runner", {}).get("runner_type") != "local":
        raise ValueError("local_agent_runner_required")
    model = profile["agent_runner"]["config"]["model"]
    model["provider_id"] = provider_id
    model["fallback_provider_ids"] = []
    settings = profile["provider_settings"]
    settings.update(
        provider_pool=[provider_id],
        display_reasoning_text=False,
        streaming_response=False,
        show_tool_use_status=False,
        show_tool_call_result=False,
    )
    # Unsupported output modes must not turn ordinary replies into failed media.
    profile["t2i"] = False
    profile["provider_tts_settings"]["enable"] = False
    platform = profile["platform_settings"]
    platform["reply_with_quote"] = True
    platform["reply_with_mention"] = False
    platform["segmented_reply"]["enable"] = False
    compression = profile["agent_runner"]["config"]["compression"]
    compression.update(
        max_turns=max_turns, trim_turns=min(4, max_turns - 1),
        overflow_strategy="truncate_by_turns", provider_id="",
    )
    # Preserve the configured plugin boundary and add only this platform's manager.
    plugins = profile.get("plugin_set", ["*"])
    if "*" not in plugins and "astrbot_plugin_lajiaoliao" not in plugins:
        profile["plugin_set"] = [*plugins, "astrbot_plugin_lajiaoliao"]
    return profile


def plan_profiles(instance_ids, routes):
    """Refuse ambiguous/shared wildcard routes instead of changing their order."""
    plan = []
    for instance in instance_ids:
        owned = {}
        for pattern, target in routes.items():
            platform = pattern.split(":", 2)[0]
            if platform == instance:
                owned[pattern] = target
            elif not platform or fnmatch.fnmatchcase(instance, platform):
                raise ValueError("shared_wildcard_route_requires_review")
        targets = set(owned.values()) - {"default"}
        if len(targets) > 1:
            raise ValueError("multiple_instance_profiles_require_review")
        source = next(iter(targets), "default")
        reuse = source != "default" and all(
            pattern.split(":", 2)[0] == instance
            for pattern, target in routes.items() if target == source
        )
        plan.append({
            "instance_id": instance, "source_profile": source,
            "reuse": reuse, "routes": list(owned), "catchall": instance + "::",
        })
    return plan


def profile_update_payload(profile):
    """The profile API merges top-level fields; omission does not delete them."""
    payload = copy.deepcopy(profile)
    payload.update(dashboard={}, platform=[], provider=[], provider_sources=[])
    return payload


def validate_provider(config, provider_id):
    provider = next(
        (row for row in config.get("provider", []) if row["id"] == provider_id),
        None,
    )
    if not provider or not provider.get("enable", False):
        raise ValueError("enabled_native_provider_required")
    source = next(
        (row for row in config.get("provider_sources", [])
         if row["id"] == provider.get("provider_source_id")),
        {},
    )
    if source.get("type", provider.get("type")) != NATIVE_TYPE:
        raise ValueError("structured_native_provider_required")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", action="append", required=True)
    parser.add_argument(
        "--provider", default="GeminiNativeLajiao/gemini-3.8-flash",
    )
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.max_turns < 2 or args.max_turns > 100:
        raise ValueError("max_turns_out_of_range")
    if args.apply and args.backup is None:
        raise ValueError("protected_backup_required")
    root = Path(__file__).resolve().parents[2]
    config_path = root / "data/cmd_config.json"
    original_bytes = config_path.read_bytes()
    config = json.loads(original_bytes.decode("utf-8-sig"))
    validate_provider(config, args.provider)
    instances = list(dict.fromkeys(args.instance))
    bots = {row["id"]: row for row in config["platform"]}
    owners = set()
    for instance in instances:
        bot = bots.get(instance, {})
        if bot.get("type") != "lajiaoliao" or not bot.get("session_ref"):
            raise ValueError("bound_lajiaoliao_instance_required")
        session = json.loads(
            (root / "data/plugin_data/astrbot_plugin_lajiaoliao"
             / bot["session_ref"] / "session.json").read_text()
        )
        if session["state"] != "bound" or session["instance_id"] != instance:
            raise ValueError("session_binding_changed")
        owners.add(session["owner"])
    if len(owners) != 1:
        raise ValueError("same_dashboard_owner_required")
    authorization = jwt.encode(
        {"username": owners.pop(), "exp": int(time.time()) + 600},
        config["dashboard"]["jwt_secret"], algorithm="HS256",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *arguments):
            return None

    client = urllib.request.build_opener(NoRedirect())

    def api(path, body=None, method=None):
        request = urllib.request.Request(
            "http://127.0.0.1:6185" + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Authorization": "Bearer " + authorization,
                     "Content-Type": "application/json"},
        )
        try:
            with client.open(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError("dashboard_http_" + str(error.code)) from None
        if result.get("status") != "ok":
            raise RuntimeError("dashboard_request_rejected")
        return result.get("data")

    routes = api("/api/v1/config-routes")["routing"]
    plan = plan_profiles(instances, routes)
    profiles = api("/api/v1/config-profiles")["info_list"]
    originals = {
        row["source_profile"]: api(
            "/api/v1/config-profiles/" + row["source_profile"]
        )["config"] for row in plan
    }
    desired = {
        key: reply_profile(value, args.provider, args.max_turns)
        for key, value in originals.items()
    }
    report = {
        "applied": False, "plan": plan, "provider_id": args.provider,
        "max_turns": args.max_turns, "steps": [],
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    args.backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(args.backup, 0o700)
    atomic_json(args.backup / "before.json", {
        "routes": routes, "profiles": originals, "profile_metadata": profiles,
    })

    def record():
        atomic_json(args.backup / "report.json", report)

    record()
    for row in plan:
        instance, source = row["instance_id"], row["source_profile"]
        if api("/api/v1/config-routes")["routing"] != routes:
            raise RuntimeError("routes_changed_refresh_required")
        if api("/api/v1/config-profiles/" + source)["config"] != originals[source]:
            raise RuntimeError("profile_changed_refresh_required")
        name = "\u8fa3\u6912\u804a\u00b7" + instance
        if row["reuse"]:
            profile_id = source
            api(
                "/api/v1/config-profiles/" + source,
                profile_update_payload(desired[source]), "PUT",
            )
            report["steps"].append({"updated_profile": profile_id})
        else:
            profile_id = api("/api/v1/config-profiles", {
                "name": name, "config": desired[source],
            })["conf_id"]
            report["steps"].append({"created_profile": profile_id})
        record()
        profile_path = root / "data/config" / ("abconf_" + profile_id + ".json")
        os.chmod(profile_path, 0o600)
        api("/api/v1/config-profiles/" + profile_id, {"name": name}, "PATCH")
        for pattern in dict.fromkeys([*row["routes"], row["catchall"]]):
            api("/api/v1/config-routes/" + quote(pattern, safe=""),
                {"config_id": profile_id}, "PUT")
            routes[pattern] = profile_id
            report["steps"].append({"route": pattern, "profile_id": profile_id})
            record()
        row["profile_id"] = profile_id
        # Preserve the new value for the next concurrency check on a reused profile.
        originals[source] = api("/api/v1/config-profiles/" + source)["config"]
    after_routes = api("/api/v1/config-routes")["routing"]
    for row in plan:
        if after_routes.get(row["catchall"]) != row["profile_id"]:
            raise RuntimeError("route_verification_failed")
        effective = api("/api/v1/config-profiles/" + row["profile_id"])["config"]
        if reply_profile(effective, args.provider, args.max_turns) != desired[row["source_profile"]]:
            raise RuntimeError("profile_verification_failed")
        if (
            any(effective.get(key) for key in ("platform", "provider", "provider_sources"))
            or effective.get("dashboard", {}).get("jwt_secret")
        ):
            raise RuntimeError("profile_transport_credentials_not_cleared")
    if hashlib.sha256(config_path.read_bytes()).digest() != hashlib.sha256(original_bytes).digest():
        raise RuntimeError("global_config_changed_review_required")
    report["applied"] = True
    report["global_config_unchanged"] = True
    record()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
