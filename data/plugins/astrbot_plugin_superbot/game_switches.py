"""Shared admission diagnostics and explicit, group-scoped game activation."""

import json

from .store import Rejected

FEATURES = {
    "wheel": "积分转盘",
    "slots": "老虎机PvP",
    "mines": "扫雷接龙",
    "k3": "积分快三",
    "duel": "双人对赌",
}


def blocker(store, chat, key, enabled, *, rollout=False, points=True):
    """Explain the first closed admission gate without changing configuration.

    Args:
        store: Isolated business store.
        chat: Exact group identifier.
        key: Fixed game identifier.
        enabled: This game's group-local switch.
        rollout: Whether new-table admission also requires rollout approval.
        points: Whether admission requires migrated group wallets.

    Returns:
        A public diagnostic, or an empty string when admission switches are open.
    """
    modules = store.get("modules", {})
    label = "老虎机" if key == "slots" else FEATURES[key]
    if not modules.get("game"):
        return "玩法中心总开关已关闭，请管理员在「全部功能启停」开启。"
    if not modules.get(key, key == "duel"):
        return f"{label}总开关已关闭，请管理员在「全部功能启停」开启。"
    if not store.db.execute(
        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
    ).fetchone():
        return "本群未登记或已停用，请管理员先在「群管理」启用本群。"
    if not enabled:
        return (
            f"本群{label}未启用，请管理员在「玩法管理 → 本群玩法启停」开启；"
            "总开关或管理员授权不会自动开启本群玩法。"
        )
    if points and not store.get("group_points_enabled", False):
        return "逐群积分配置尚未完成，请管理员核查；未提交、未扣分。"
    if rollout and str(chat) not in store.get("games_v3_groups", []):
        return (
            f"本群{label}开关已开启，但新版开放尚未确认；"
            "请管理员重新预览并确认开启本群玩法，已有桌次继续按原规则完成。"
        )
    return ""


def snapshot(store, chat):
    """Capture versioned local settings for overview and optimistic confirmation.

    Args:
        store: Isolated business store with game migrations initialized.
        chat: Exact registered group identifier.

    Returns:
        An immutable-comparable snapshot without wallets, orders or messages.
    """
    from .wheel import DEFAULT

    group = store.db.execute(
        "SELECT enabled,version FROM mod_groups WHERE chat=?", (str(chat),)
    ).fetchone()
    if not group:
        raise Rejected("群未登记。")
    modules = store.db.execute(
        "SELECT value,version FROM settings WHERE key='modules'"
    ).fetchone()
    features = {}
    for key in FEATURES:
        row = store.db.execute(
            f"SELECT * FROM {key}_groups WHERE chat=?", (str(chat),)
        ).fetchone()
        if key == "wheel":
            config = json.loads(row["config"]) if row else dict(DEFAULT)
            features[key] = {
                "enabled": bool(config["enabled"]),
                "version": row["version"] if row else 0,
                "config": config,
            }
        else:
            features[key] = {
                "enabled": bool(row["enabled"]) if row else False,
                "version": row["version"] if row else 0,
            }
    return {
        "group": dict(group),
        "modules": json.loads(modules["value"]) if modules else {},
        "modules_version": modules["version"] if modules else 0,
        "points": bool(store.get("group_points_enabled", False)),
        "rollout": str(chat) in store.get("games_v3_groups", []),
        "features": features,
    }


def approve_rollout(store, db, actor, chat):
    """Approve only the confirmed group's new tables while preserving other groups.

    Args:
        store: Isolated business store.
        db: Already-open transaction connection.
        actor: Verified game administrator.
        chat: Exact registered and authorized group identifier.
    """
    store.require(actor, "game", db=db, chat=chat)
    store.require(actor, "moderation", db=db, chat=chat)
    if not db.execute(
        "SELECT 1 FROM mod_groups WHERE chat=? AND enabled=1", (str(chat),)
    ).fetchone():
        raise Rejected("本群已停用，请重新预览。")
    if (
        not store.allowed(actor, "manager", db=db)
        and not db.execute(
            "SELECT 1 FROM tenant_groups g JOIN tenants t ON t.id=g.tenant WHERE g.chat=? AND t.owner=? AND g.status='active' AND t.status='active'",
            (str(chat), str(actor)),
        ).fetchone()
        and not db.execute(
            "SELECT 1 FROM mod_acl WHERE uid=? AND chat=?", (str(actor), str(chat))
        ).fetchone()
    ):
        raise Rejected("没有此群的授权。")
    groups = store.get("games_v3_groups", [], db=db)
    if not isinstance(groups, list) or any(
        not isinstance(item, str) for item in groups
    ):
        raise Rejected("新版开放范围配置异常，请管理员核查。")
    if str(chat) not in groups:
        store.put(db, "games_v3_groups", [*groups, str(chat)])
        store.audit(db, actor, "game_group_rollout", {"chat": str(chat)})


def enable_group(store, actor, chat, expected):
    """Atomically configure the five independent games without changing globals.

    Args:
        store: Isolated business store.
        actor: Administrator whose Telegram rights were just verified.
        chat: Exact group identifier.
        expected: Overview snapshot presented at confirmation.
    """
    with store.tx() as db:
        store.require(actor, "game", db=db, chat=chat)
        if snapshot(store, chat) != expected:
            raise Rejected("玩法配置已变化，请重新预览；未修改任何开关。")
        if not expected["points"]:
            raise Rejected("请先完成逐群积分配置；未修改任何开关。")
        approve_rollout(store, db, actor, chat)
        for key in FEATURES:
            if expected["features"][key]["enabled"]:
                continue
            if key == "wheel":
                config = {**expected["features"][key]["config"], "enabled": True}
                db.execute(
                    "INSERT INTO wheel_groups(chat,config,version) VALUES(?,?,1) "
                    "ON CONFLICT(chat) DO UPDATE SET config=excluded.config,version=wheel_groups.version+1",
                    (str(chat), json.dumps(config, ensure_ascii=False)),
                )
            else:
                db.execute(
                    f"INSERT INTO {key}_groups(chat,enabled,version) VALUES(?,1,1) "
                    f"ON CONFLICT(chat) DO UPDATE SET enabled=1,version={key}_groups.version+1",
                    (str(chat),),
                )
        store.audit(
            db,
            actor,
            "game_group_enable_all",
            {"chat": str(chat), "features": list(FEATURES), "rollout": True},
        )
