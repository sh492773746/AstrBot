"""Versioned daily group admission schedules; existing liabilities keep running."""

from datetime import datetime
from zoneinfo import ZoneInfo

from .game_registry import ENTERTAINMENT
from .store import Rejected

GAMES = ENTERTAINMENT


def parse_time(text, *, end=False):
    """Parse one clock value, allowing 24:00 only as the end boundary."""
    parts = text.strip().replace("：", ":").split(":")
    if len(parts) != 2 or any(not p.isascii() or not p.isdigit() for p in parts):
        raise Rejected("请发送时间，例如 09:00；结束时间可以填写 24:00。")
    hour, minute = map(int, parts)
    if hour == 24 and minute == 0 and end:
        return 1440
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise Rejected("时间范围为 00:00—24:00，24:00 只能作为结束时间。")
    return hour * 60 + minute


def clock_text(minutes):
    """Format a stored minute boundary."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def snapshot(store, chat):
    """Read group schedule data and its optimistic version."""
    row = store.db.execute(
        "SELECT version FROM settings WHERE key=?", ("game_schedule:" + str(chat),)
    ).fetchone()
    return store.get("game_schedule:" + str(chat), {}), row[0] if row else 0


def reason(store, chat, game):
    """Explain closed admission in Beijing time without mutating live games."""
    if not chat or game not in GAMES:
        return ""
    data, _ = snapshot(store, chat)
    window = data.get(game, [0, 1440]) if isinstance(data, dict) else None
    if (
        not isinstance(window, list)
        or len(window) != 2
        or any(type(value) is not int for value in window)
        or not 0 <= window[0] < 1440
        or not 0 <= window[1] <= 1440
        or window[0] == window[1]
    ):
        return "本群娱乐开放时段配置异常，请管理员核查；未提交、未扣分。"
    start, end = window
    now = datetime.fromtimestamp(store.clock(), ZoneInfo("Asia/Shanghai"))
    minute = now.hour * 60 + now.minute
    opened = start <= minute < end if start < end else minute >= start or minute < end
    if opened:
        return ""
    return (
        f"本群{GAMES[game]}当前未到开放时间；每日 "
        f"{clock_text(start)}—{clock_text(end)}（北京时间"
        f"{'，跨日' if start > end else ''}），未提交、未扣分。"
    )


def save(store, uid, chat, game, start, end, version):
    """Save one verified group's schedule atomically with version and audit."""
    if game not in GAMES or any(type(v) is not int for v in (start, end)):
        raise Rejected("无效玩法时段。")
    if not 0 <= start < 1440 or not 0 <= end <= 1440 or start == end:
        raise Rejected("起止时间不能相同；全天开放请设置 00:00—24:00。")
    with store.tx() as db:
        store.require(uid, "game", db=db, chat=chat)
        store.require(uid, "moderation", db=db, chat=chat)
        current, actual = snapshot(store, chat)
        if actual != version:
            raise Rejected("本群时段已变化，请重新打开设置。")
        store.put(db, "game_schedule:" + str(chat), {**current, game: [start, end]})
        store.audit(
            db,
            uid,
            "game_schedule",
            {"chat": str(chat), "game": game, "start": start, "end": end},
        )


async def action(ui, update, payload):
    """Recheck local Telegram administrators before schedule access."""
    from .tenants import group_admin

    async with group_admin(
        ui.runtime, str(update.effective_user.id), str(payload["chat"])
    ):
        return await _action(ui, update, payload)


async def _action(ui, update, payload):
    """Offer group-scoped schedule selection, stepped input and confirmation."""
    if update.effective_chat.type != "private":
        raise Rejected("请私聊设置本群玩法时段。")
    uid, chat = str(update.effective_user.id), str(payload["chat"])
    ui.store.require(uid, "game", chat=chat)
    from .tenants import platform_group

    if platform_group(ui.store, chat):
        await ui.runtime.moderation.check(uid, chat, "view", enabled=False)
    else:
        await ui.runtime.tenants.verify(uid, chat, "game")
    data, version = snapshot(ui.store, chat)
    back = [("返回每日娱乐时段", {"action": "schedule_home", "chat": chat})]
    name = payload["action"]
    if name == "schedule_home":
        ui.store.clear_dialog(uid)
        title = ui.store.db.execute(
            "SELECT title FROM mod_groups WHERE chat=?", (chat,)
        ).fetchone()
        lines = []
        for game, label in GAMES.items():
            start, end = data.get(game, [0, 1440])
            lines.append(f"{label}：{clock_text(start)}—{clock_text(end)}")
        return await ui.render(
            update,
            f"🕒 **{title[0] if title else chat} · 每日娱乐时段**\n"
            + "\n".join(lines)
            + "\n\n北京时间；起点包含、终点不包含。结束早于开始表示跨日。"
            "\n全天开放：00:00—24:00。分别填写开始、结束，再确认保存。"
            "\n仅限制六项娱乐的新参与，不控制广告、积分奖励、AI客服或制图。已有订单继续结算，手动关闭不会到点强制开启。",
            [
                (label, {"action": "schedule_edit", "chat": chat, "game": game})
                for game, label in GAMES.items()
            ]
            + [
                (
                    "返回上一级",
                    {
                        "action": "games_group_view"
                        if platform_group(ui.store, chat)
                        else "tenant_switches",
                        "chat": chat,
                    },
                )
            ],
            fold_sections=True,
        )
    game = payload.get("game")
    if game not in GAMES:
        raise Rejected("无效玩法。")
    if name == "schedule_edit":
        ui.store.dialog(
            uid,
            {"form": "game_schedule", "chat": chat, "game": game, "version": version},
        )
        return await ui.render(
            update,
            f"🕒 **{GAMES[game]} · 第1/2步**\n发送每日开始时间（00:00—23:59），例如 09:00。\n全天开放请先发00:00，下一步发24:00。北京时间。\n/cancel 取消。",
            back,
        )
    if name == "schedule_save":
        save(
            ui.store,
            uid,
            chat,
            game,
            payload["start"],
            payload["end"],
            payload["version"],
        )
        return await action(ui, update, {"action": "schedule_home", "chat": chat})
    raise Rejected("此时段操作已失效，请重新打开。")


async def input_text(ui, update, dialog):
    """Collect start and end separately without writing partial configuration."""
    uid = str(update.effective_user.id)
    if dialog.get("game") not in GAMES:
        ui.store.clear_dialog(uid)
        raise Rejected("旧时段表单已停用，请打开【每日娱乐时段】重新选择娱乐玩法。")
    end_step = "start" in dialog
    value = parse_time(update.message.text or "", end=end_step)
    if not end_step:
        ui.store.dialog(uid, {**dialog, "start": value})
        return await ui.render(
            update,
            "🕒 **第2/2步**\n发送每日结束时间（00:00—24:00），例如 24:00。\n早于开始时间表示跨日，不能与开始时间相同。\n/cancel 取消。",
            [
                (
                    "返回上一级",
                    {
                        "action": "schedule_edit",
                        "chat": dialog["chat"],
                        "game": dialog["game"],
                    },
                )
            ],
        )
    if value == dialog["start"]:
        raise Rejected("起止时间不能相同；全天开放请设置 00:00—24:00。")
    ui.store.clear_dialog(uid)
    return await ui.render(
        update,
        f"🕒 **确认本群{GAMES[dialog['game']]}时段**\n"
        f"{clock_text(dialog['start'])}—{clock_text(value)}（北京时间）\n"
        "只影响新参与，不取消已有订单。",
        [
            ("确认保存", {**dialog, "action": "schedule_save", "end": value}),
            ("取消", {"action": "schedule_home", "chat": dialog["chat"]}),
        ],
    )
