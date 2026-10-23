"""Global Beijing-time admission policy; settlement never depends on this gate."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Rejected

KEY = "game_hours"
DEFAULT = {"mode": "all", "start": "20:00", "end": "02:00"}
TZ = ZoneInfo("Asia/Shanghai")


def is_open(store, db=None):
    """Evaluate the current admission window without creating scheduler tasks.

    Args:
        store: Instance store with injectable clock.
        db: Optional active transaction.

    Returns:
        Whether new bets may be admitted.
    """
    config = store.get(KEY, DEFAULT, db)
    if config["mode"] == "all":
        return True
    if config["mode"] != "daily":
        return False
    current = datetime.fromtimestamp(store.clock(), TZ).strftime("%H:%M")
    start, end = config["start"], config["end"]
    return start <= current < end if start < end else current >= start or current < end


def description(store, db=None):
    config = store.get(KEY, DEFAULT, db)
    if config["mode"] == "all":
        return "所有群：全天开放"
    if config["mode"] == "paused":
        return "所有群：暂停新下注"
    return f"所有群：每天 {config['start']}—{config['end']}（北京时间）"


async def action(ui, update, payload, token=""):
    """Select times through bound buttons and save a versioned global policy.

    Args:
        ui: Existing private UI.
        update: Telegram update.
        payload: Bound callback data.
        token: Single-use confirmation token.
    """
    store = ui.store
    uid, chat = str(update.effective_user.id), str(update.effective_chat.id)
    store.require(uid, "game")
    if update.effective_chat.type != "private":
        raise Rejected("请在私聊设置开放时间。")
    config = store.get(KEY, DEFAULT)
    version_row = store.db.execute(
        "SELECT version FROM settings WHERE key=?", (KEY,)
    ).fetchone()
    version = version_row[0] if version_row else 0
    kind = payload["action"]
    back = [("返回开放时间", {"action": "hours_home"})]
    if kind == "hours_save":
        proposed = payload["config"]
        if proposed.get("mode") not in {"all", "daily", "paused"} or any(
            not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", proposed.get(key, ""))
            for key in ("start", "end")
        ):
            raise Rejected("时间设置无效。")
        if proposed["mode"] == "daily" and proposed["start"] == proposed["end"]:
            raise Rejected("开始和结束时间不能相同，请选择全天开放或调整时间。")
        with store.tx() as db:
            store.require(uid, "game", db)
            store.resolve(token, uid, chat)
            if payload["version"] != version:
                raise Rejected("设置已变化，请重新选择。")
            if not db.execute(
                "UPDATE callbacks SET used=1 WHERE token=? AND used=0", (token,)
            ).rowcount:
                raise Rejected("已处理这次确认。")
            store.put(db, KEY, proposed)
            store.audit(db, uid, "game_hours", proposed)
        config = proposed
        kind = "hours_home"
    if kind == "hours_home":
        return await ui.render(
            update,
            description(store)
            + "\n当前："
            + ("允许新下注" if is_open(store) else "休息中")
            + "\n统一控制所有群。休息期间停止新下注及开盘播报；已下注订单照常结算，开奖采集继续。"
            + "\n设置确认后立即按当前时间生效，不改变倍率和原有总开关。",
            [
                (
                    "全天开放",
                    {
                        "action": "hours_preview",
                        "config": {**config, "mode": "all"},
                        "version": version,
                    },
                ),
                (
                    "每日定时",
                    {
                        "action": "hours_hour",
                        "field": "start",
                        "config": {**config, "mode": "daily"},
                        "version": version,
                    },
                ),
                (
                    "暂停开放",
                    {
                        "action": "hours_preview",
                        "config": {**config, "mode": "paused"},
                        "version": version,
                    },
                ),
                ("返回模拟28管理", {"action": "admin_game"}),
            ],
        )
    proposed = payload["config"]
    if payload["version"] != version:
        raise Rejected("设置已变化，请重新打开开放时间。")
    if kind == "hours_preview":
        if proposed["mode"] == "daily" and proposed["start"] == proposed["end"]:
            raise Rejected("开始与结束时间不能相同。")
        text = {
            "all": "全天开放",
            "paused": "暂停新下注",
            "daily": f"每天 {proposed['start']}—{proposed['end']}（北京时间）",
        }[proposed["mode"]]
        return await ui.render(
            update,
            f"确认所有群统一设置为：{text}？\n立即按当前时间生效，已受理下注照常结算。",
            [("确认保存", {**payload, "action": "hours_save"})] + back,
        )
    field = payload["field"]
    if field not in {"start", "end"}:
        raise Rejected("时间项无效。")
    title = "开始" if field == "start" else "结束"
    if kind == "hours_hour":
        page = min(1, max(0, int(payload.get("page", 0))))
        choices = [
            (
                f"{hour:02d}点",
                {**payload, "action": "hours_minute", "hour": hour, "page": 0},
            )
            for hour in range(page * 12, page * 12 + 12)
        ]
        choices.append(
            ("00–11点" if page else "12–23点", {**payload, "page": 1 - page})
        )
        return await ui.render(
            update, f"选择每日{title}小时（北京时间）", choices + back
        )
    if kind == "hours_minute":
        page = min(3, max(0, int(payload.get("page", 0))))
        choices = []
        for minute in range(page * 15, page * 15 + 15):
            value = f"{payload['hour']:02d}:{minute:02d}"
            chosen = {**proposed, field: value}
            target = {
                "action": "hours_hour" if field == "start" else "hours_preview",
                "config": chosen,
                "version": version,
                "field": "end",
            }
            choices.append((value, target))
        if page:
            choices.append(("上一页", {**payload, "page": page - 1}))
        if page < 3:
            choices.append(("下一页", {**payload, "page": page + 1}))
        choices.append(("重新选小时", {**payload, "action": "hours_hour", "page": 0}))
        return await ui.render(update, f"选择每日{title}分钟", choices + back)
    raise Rejected("时间操作无效。")
