"""Shared read-only admission snapshot for public group navigation."""

import json

from telegram.error import TelegramError

from .game_switches import FEATURES, blocker
from .tenants import binding, local_config


async def refresh(runtime, chat):
    """Cache verified navigation permissions briefly; unknown checks fail closed."""
    chat = str(chat)
    if not hasattr(runtime, "navigation_permissions"):
        runtime.navigation_permissions = {}
    previous = runtime.navigation_permissions.get(chat, {})
    if previous.get("expires", 0) > runtime.store.clock():
        return previous
    state = {
        "ready": False,
        "reason": "权限尚未核实",
        "expires": runtime.store.clock() + 30,
    }
    try:
        member = await runtime.bot.get_chat_member(chat, runtime.bot.id)
        state["ready"] = member.status == "creator" or (
            member.status == "administrator"
            and bool(getattr(member, "can_delete_messages", False))
        )
        state["reason"] = "" if state["ready"] else "机器人需具备管理员及删除消息权限"
    except (TelegramError, TimeoutError, ConnectionError):
        state["reason"] = "Telegram权限核验失败，暂不展示业务入口"
    runtime.navigation_permissions[chat] = state
    return state


def snapshot(runtime, chat):
    """Resolve stored gates without spending budgets or mutating configuration."""
    store = runtime.store
    chat = str(chat)
    tables = {
        r[0]
        for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    result = dict.fromkeys(
        (*FEATURES, "activate", "points", "avatar", "rules", "notes", "reports"), False
    )
    group = store.db.execute(
        "SELECT enabled FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()
    if not group or not group[0]:
        return result
    permissions = getattr(runtime, "navigation_permissions", {}).get(chat, {})
    if not permissions.get("ready") or permissions.get("expires", 0) <= store.clock():
        return result
    owner = binding(store, chat) if "tenant_groups" in tables else None
    if owner and (
        owner["status"] != "active"
        or owner["tenant_status"] != "active"
        or owner["error"]
    ):
        return result
    modules = store.get("modules", {})
    for key in FEATURES:
        if f"{key}_groups" not in tables:
            continue
        row = store.db.execute(
            f"SELECT * FROM {key}_groups WHERE chat=?", (chat,)
        ).fetchone()
        enabled = bool(
            row
            and (
                json.loads(row["config"]).get("enabled")
                if key == "wheel"
                else row["enabled"]
            )
        )
        result[key] = not blocker(
            store,
            chat,
            key,
            enabled,
            rollout=key in {"slots", "mines"},
            points=key not in {"duel", "k3"},
        )
    result["points"] = bool(modules.get("points"))
    result["activate"] = bool(
        modules.get("game")
        and result["points"]
        and (
            not owner
            or owner["tenant"] == "platform"
            or local_config(store, chat, "canada_enabled", False)
        )
    )
    if modules.get("moderation") and hasattr(runtime, "community"):
        policy = runtime.community.policy(chat)["config"]["enabled"]
        result.update(
            {key: bool(policy.get(key)) for key in ("rules", "notes", "reports")}
        )
        avatar = getattr(runtime, "avatar", None)
        result["avatar"] = bool(avatar and avatar.enabled())
        if result["avatar"] and owner and owner["tenant"] != "platform":
            grant = store.db.execute(
                "SELECT enabled,budget,used FROM tenant_resource_grants WHERE chat=? AND resource='avatar'",
                (chat,),
            ).fetchone()
            result["avatar"] = bool(
                grant and grant["enabled"] and grant["used"] < grant["budget"]
            )
    return result
