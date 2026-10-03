"""Owner-only reusable group presets with atomic, version-bound application."""

import asyncio
import copy
import json
from pathlib import Path

from .ad_killer import ACTIONS, RULES, validate
from .community import FEATURES
from .store import Rejected, encode

KEY = "owner_group_template"
LOCAL_LISTS = ("users", "domains", "telegram_allow")


async def action(ui, update, payload, token=""):
    """Preview, capture or atomically apply a preset to one authorized group.

    Args:
        ui: Existing private menu renderer.
        update: Authenticated Telegram interaction.
        payload: Server-stored, user/chat-bound callback.
        token: Ten-minute, single-use confirmation identifier.

    Raises:
        Rejected: Authority, source version, target version or permissions changed.
    """
    runtime, store = ui.runtime, ui.store
    uid, chat = str(update.effective_user.id), str(payload["chat"])
    if update.effective_chat.type != "private":
        raise Rejected("超管模板仅可在私聊使用")
    store.require(uid, "manager")
    await runtime.moderation.check(uid, chat, "view")
    group = runtime.moderation.row(chat)
    cm = runtime.community.policy(chat)
    ak = runtime.ad_killer.policy(chat)
    broadcast = store.db.execute(
        "SELECT * FROM gb_policy WHERE chat=?", (chat,)
    ).fetchone()
    saved = store.db.execute(
        "SELECT value,version FROM settings WHERE key=?", (KEY,)
    ).fetchone()
    revision = saved["version"] if saved else 0
    versions = [
        cm["version"],
        ak["version"],
        broadcast["version"] if broadcast else 0,
        group["version"],
    ]
    op = payload["action"]
    back = [
        ("返回模板", {"action": "mod_tpl_home", "chat": chat}),
        ("返回群管理", {"action": "mod_group", "chat": chat}),
    ]
    if op == "mod_tpl_home":
        buttons = [
            (
                "套用内置基础模板",
                {"action": "mod_tpl_preview", "chat": chat, "mode": "default"},
            )
        ]
        if saved:
            buttons.append(
                (
                    "套用我的超管模板",
                    {"action": "mod_tpl_preview", "chat": chat, "mode": "saved"},
                )
            )
        buttons += [
            (
                "将本群保存为我的模板",
                {"action": "mod_tpl_preview", "chat": chat, "mode": "capture"},
            ),
            ("返回群管理", {"action": "mod_group", "chat": chat}),
        ]
        return await ui.render(
            update,
            f"超管群配置模板 · {group['title']}\n"
            "先把一个群调好，再保存为模板，进入其他已启用群一键套用。\n"
            f"我的模板：{'已保存，版本' + str(revision) if saved else '尚未保存'}\n"
            "内置模板开启举报、欢迎、群规、常用说明和人工警告入口；"
            "不改变广告杀手。全局模块未开启时对应功能仍不可用。\n"
            "我的模板另外复制广告杀手开关、检测项、处罚和关键词；套用前逐项预览。\n"
            "不复制管理员授权、白名单、日志频道、入群验证、自动审理、定时禁言或任何资金设置。",
            buttons,
        )
    mode = payload.get("mode")
    if mode == "default":
        content = json.loads(
            Path(__file__)
            .with_name("community_defaults.json")
            .read_text(encoding="utf-8")
        )
        content.pop("log")
        proposed = {"schema": 1, "content": content, "broadcast": False, "killer": None}
    elif mode == "saved":
        if not saved:
            raise Rejected("请先保存超管模板")
        proposed = json.loads(saved["value"])
    elif mode == "capture":
        content = copy.deepcopy(cm["config"])
        content.pop("log")
        config = json.loads(ak["config"])
        for name in LOCAL_LISTS:
            config.pop(name)
        proposed = {
            "schema": 1,
            "content": content,
            "broadcast": False,
            "killer": {"enabled": bool(ak["enabled"]), "config": config},
        }
    else:
        raise Rejected("模板入口无效")
    proposed["broadcast"] = False
    if proposed.get("schema") != 1:
        raise Rejected("模板版本不兼容，请重新保存")
    content = {
        **copy.deepcopy(proposed["content"]),
        "log": copy.deepcopy(cm["config"]["log"]),
    }
    runtime.community.validate(content)
    if type(proposed.get("broadcast")) is not bool:
        raise Rejected("模板开关无效")
    killer = copy.deepcopy(proposed["killer"])
    if killer is not None:
        local = json.loads(ak["config"])
        for name in LOCAL_LISTS:
            killer["config"][name] = local[name]
        validate(killer["config"])
        if type(killer["enabled"]) is not bool:
            raise Rejected("模板广告杀手开关无效")
        if killer["enabled"] and ak["error"]:
            raise Rejected("广告杀手存在待核查故障，请先处理，模板不能清除故障")
    if op in {"mod_tpl_details", "mod_tpl_preview"}:
        if op == "mod_tpl_details":
            return await ui.render(
                update,
                "模板正文预览\n"
                + content["welcome"]
                + "\n\n"
                + content["rules"]
                + "\n\n"
                + "\n\n".join(
                    n["title"]
                    + "\n"
                    + n["body"]
                    + ("\n" + n["url"] if n["url"] else "")
                    for n in content["notes"]
                ),
                back,
            )
        lines = [
            f"{'保存本群为超管模板' if mode == 'capture' else '套用模板'} · {group['title']}"
        ]
        lines += [
            f"{label}：{'开' if cm['config']['enabled'][key] else '关'} → {'开' if content['enabled'][key] else '关'}"
            for key, label in FEATURES.items()
        ]
        lines += [
            "加拿大28周期公告已停用，模板不再启用播报。",
            "欢迎文案、群规、常用说明将"
            + ("保存到模板。" if mode == "capture" else "按模板替换，可随后逐群更改。"),
        ]
        if killer is None:
            lines += ["广告杀手：保持目标群现状。"]
        else:
            cfg = killer["config"]
            lines += [f"广告杀手总开关：{'开' if killer['enabled'] else '关'}"]
            lines += [
                f"{RULES[k]}：{'开' if r['enabled'] else '关'} · {ACTIONS[r['action']]}"
                for k, r in cfg["rules"].items()
            ]
            lines += [
                f"重复：{cfg['repeat_seconds']}秒/{cfg['repeat_count']}条；刷屏：{cfg['flood_seconds']}秒/{cfg['flood_count']}条；禁言{cfg['mute_minutes']}分钟。",
                f"累计升级：{'开' if cfg['escalation']['enabled'] else '关'}，{cfg['escalation_hours']}小时/{cfg['escalation']['count']}次，{ACTIONS[cfg['escalation']['action']]}。",
                "关键词：" + (encode(cfg["keywords"]) if cfg["keywords"] else "未设置"),
                "启用后命中会直接执行，可能误判；踢出后可重新加入，封禁需先解封且可能删除历史消息。",
            ]
        lines += [
            "管理员授权、目标群白名单、日志频道、入群验证、10人自动审理、定时禁言、收款和积分设置均不变。",
            "保存模板不会更改本群；套用只影响当前选中的群，不自动同步到其他群。"
            if mode == "capture"
            else "确认后仅修改本群配置，不发送测试消息，不立即处罚已有消息。",
        ]
        return await ui.render(
            update,
            "\n".join(lines),
            [
                (
                    "确认保存模板" if mode == "capture" else "确认套用到本群",
                    {
                        "action": "mod_tpl_confirm",
                        "chat": chat,
                        "mode": mode,
                        "revision": revision,
                        "versions": versions,
                        "proposed": proposed,
                    },
                ),
                (
                    "查看欢迎、群规和说明",
                    {"action": "mod_tpl_details", "chat": chat, "mode": mode},
                ),
            ]
            + back,
        )
    if op != "mod_tpl_confirm":
        raise Rejected("模板操作无效")
    # Both writers use this lock order. No network call occurs inside the transaction.
    async with runtime.moderation.locks.setdefault(chat, asyncio.Lock()):
        async with runtime.group_game.broadcast.locks[chat]:
            await runtime.moderation.check(uid, chat, "view")
            if mode != "capture" and killer and killer["enabled"]:
                await runtime.moderation.check(uid, chat, "delete")
                cfg = killer["config"]
                if any(
                    r["enabled"] and r["action"] != "delete"
                    for r in cfg["rules"].values()
                ) or (
                    cfg["escalation"]["enabled"]
                    and cfg["escalation"]["action"] != "delete"
                ):
                    await runtime.moderation.check(uid, chat, "mute")
            with store.tx() as db:
                store.require(uid, "manager", db=db)
                current = db.execute(
                    "SELECT version FROM settings WHERE key=?", (KEY,)
                ).fetchone()
                bp = db.execute(
                    "SELECT version FROM gb_policy WHERE chat=?", (chat,)
                ).fetchone()
                actual = [
                    runtime.community.policy(chat)["version"],
                    runtime.ad_killer.policy(chat)["version"],
                    bp[0] if bp else 0,
                    runtime.moderation.row(chat)["version"],
                ]
                if (
                    (current[0] if current else 0) != payload.get("revision")
                    or actual != payload.get("versions")
                    or proposed != payload.get("proposed")
                ):
                    raise Rejected("模板或目标群配置已变化，请重新预览")
                if (
                    killer
                    and killer["enabled"]
                    and runtime.ad_killer.policy(chat)["error"]
                ):
                    raise Rejected("广告杀手有待核查故障")
                if not db.execute(
                    "UPDATE callbacks SET used=1 WHERE token=? AND uid=? AND chat=? AND used=0 AND expires>?",
                    (token, uid, str(update.effective_chat.id), store.clock()),
                ).rowcount:
                    raise Rejected("确认已过期或使用")
                if mode == "capture":
                    store.put(db, KEY, proposed)
                else:
                    db.execute(
                        "INSERT INTO cm_config(chat,actor,version,config) VALUES(?,?,1,?) ON CONFLICT(chat) DO UPDATE SET actor=excluded.actor,version=cm_config.version+1,config=excluded.config",
                        (chat, uid, encode(content)),
                    )
                    if killer:
                        db.execute(
                            "INSERT INTO ak_policies(chat,enabled,actor,config) VALUES(?,?,?,?) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled,actor=excluded.actor,config=excluded.config,version=ak_policies.version+1",
                            (
                                chat,
                                int(killer["enabled"]),
                                uid,
                                encode(killer["config"]),
                            ),
                        )
                store.audit(
                    db,
                    uid,
                    "owner_template_capture"
                    if mode == "capture"
                    else "owner_template_apply",
                    {
                        "chat": chat,
                        "mode": mode,
                        "before": {
                            "community": cm["config"],
                            "broadcast": bool(broadcast and broadcast["enabled"]),
                            "killer": ak,
                        },
                        "template": proposed,
                    },
                )
    return await ui.render(
        update,
        "超管模板已保存，可进入其他群套用。"
        if mode == "capture"
        else "本群配置已套用。后续可在各功能中单独修改；没有复制授权、资金或白名单。",
        back,
    )
