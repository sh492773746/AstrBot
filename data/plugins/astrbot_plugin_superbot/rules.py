"""Pure Canada28 rules independently ported from the reviewed Go baseline."""

from .store import Rejected

VERSION = "go-canada-20260922-v1"
BASIC = {"big", "small", "odd", "even"}
COMBO = {"big_odd", "big_even", "small_odd", "small_even"}
COVERAGE = {
    "big": 3,
    "small": 12,
    "odd": 5,
    "even": 10,
    "big_odd": 1,
    "big_even": 2,
    "small_odd": 4,
    "small_even": 8,
}
LABELS = {
    "大": "big",
    "小": "small",
    "单": "odd",
    "双": "even",
    "大单": "big_odd",
    "大双": "big_even",
    "小单": "small_odd",
    "小双": "small_even",
    "极大": "extreme_big",
    "极小": "extreme_small",
    "龙": "dragon",
    "虎": "tiger",
    "豹": "leopard_type",
    "对子": "pair",
    "顺子": "straight",
    "豹子": "triple",
}
NAMES = {
    "room18": "1.8倍",
    "special": "1.88倍",
    "double": "2.0倍",
    "room27": "2.7倍",
    "room28": "2.8倍",
    "room32": "3.2倍",
}
NEW_ODDS = {"edge_small": 440, "edge_big": 440, "edge": 210, "middle": 170}
NEW_CAPS = dict.fromkeys(NEW_ODDS, 500)
LABELS.update({"小边": "edge_small", "大边": "edge_big", "边": "edge", "中": "middle"})
for position in "abc":
    for label, suffix, odds, cap in (
        ("大", "big", 199, 2000),
        ("小", "small", 199, 2000),
        ("单", "odd", 199, 2000),
        ("双", "even", 199, 2000),
        ("小单", "small_odd", 490, 1000),
        ("大双", "big_even", 490, 1000),
        ("大单", "big_odd", 320, 1000),
        ("小双", "small_even", 320, 1000),
    ):
        key = f"pos_{position}_{suffix}"
        LABELS[position + label] = key
        NEW_ODDS[key], NEW_CAPS[key] = odds, cap
    for digit in range(10):
        key = f"pos_{position}_number_{digit}"
        LABELS[f"{position}{digit}点"] = key
        NEW_ODDS[key], NEW_CAPS[key] = 980, 100
PLAY_NAMES = {value: key for key, value in LABELS.items()} | {
    f"number_{i}": f"数字{i}" for i in range(28)
}
STATUS_NAMES = {
    "pending": "待开奖",
    "win": "中奖",
    "won": "中奖",
    "lose": "未中奖",
    "refund": "回本",
    "cancelled": "已取消",
}


def room_defaults():
    """Return independent room snapshots, converting Go cents to whole points.

    Returns:
        Six disabled Canada28 rooms with exact hundredth multipliers.
    """
    rooms = {}
    number_odds = [488, 128, 88, 58, 48, 38, 28, 18, 15, 15, 14, 13, 12, 11]
    for room, name in NAMES.items():
        odds = dict.fromkeys(BASIC, 200)
        odds.update(
            big_odd=425,
            small_even=425,
            big_even=465,
            small_odd=465,
            extreme_big=1500,
            extreme_small=1500,
            dragon=285,
            tiger=285,
            leopard_type=285,
            pair=320,
            straight=1400,
            triple=6600,
        )
        if room in {"double", "room28"}:
            odds.update(NEW_ODDS)
        if room == "double":
            odds.update(big_odd=420, small_even=420, big_even=460, small_odd=460)
        if room in {"room27", "room28", "room32"}:
            odds.update(
                {p: {"room27": 270, "room28": 280, "room32": 320}[room] for p in BASIC}
            )
            odds.update(dict.fromkeys(COMBO, 600))
        if room == "room27":
            odds.update(
                extreme_big=1200,
                extreme_small=1200,
                dragon=280,
                tiger=280,
                leopard_type=280,
                pair=300,
                straight=1500,
            )
        if room == "room32":
            odds.update(big_odd=650, small_even=650, big_even=700, small_odd=700)
        odds.update(
            {f"number_{i}": number_odds[min(i, 27 - i)] * 100 for i in range(28)}
        )
        limits = dict.fromkeys(BASIC, 100000)
        limits.update(dict.fromkeys(COMBO, 30000))
        limits.update(
            extreme_big=10000,
            extreme_small=10000,
            dragon=10000,
            tiger=10000,
            leopard_type=10000,
            pair=30000,
            straight=10000,
            triple=2000,
        )
        if room == "room27":
            limits.update(dict.fromkeys(BASIC, 30000))
            limits.update(dict.fromkeys(COMBO, 10000))
            limits.update(
                dragon=20000,
                tiger=20000,
                leopard_type=20000,
                pair=20000,
                straight=20000,
                triple=5000,
            )
        limits.update(
            {
                f"number_{i}": [1000, 2000, 5000][min(i, 27 - i)]
                if min(i, 27 - i) < 3
                else 10000
                for i in range(28)
            }
        )
        maximum = 200000 if room in {"room18", "special", "room27"} else 300000
        rooms[room] = {
            "id": room,
            "name": name,
            "enabled": False,
            "minimum": 1,
            "maximum": maximum,
            "total": maximum,
            "odds": odds,
            "single_limits": dict(NEW_CAPS),
            "additional_rules_version": "positions-edges-20260925-v1",
            "limits": limits,
            "version": VERSION,
        }
    return rooms


def canada_balls(raw):
    if (
        len(raw) != 20
        or any(type(n) is not int or not 1 <= n <= 80 for n in raw)
        or len(set(raw)) != 20
    ):
        raise Rejected("Keno 原始号码无效")
    numbers = sorted(raw)
    return [sum(numbers[start:19:3]) % 10 for start in (1, 2, 3)]


def evaluate(room, play, amount, balls):
    """Evaluate one winning/refund rule using integer arithmetic.

    Args:
        room: Immutable accepted-order snapshot.
        play: Canonical play key.
        amount: Positive whole points.
        balls: Three Canada28 digits.

    Returns:
        Disposition and total payout including principal.
    """
    if (
        len(balls) != 3
        or any(type(n) is not int or not 0 <= n <= 9 for n in balls)
        or amount <= 0
        or play not in room["odds"]
    ):
        raise Rejected("结算参数无效")
    a, b, c = sorted(balls)
    total = sum(balls)
    big, odd = total >= 14, total % 2 == 1
    triple = a == c
    pair = not triple and (a == b or b == c)
    straight = (b - a == 1 and c - b == 1) or [a, b, c] in ([0, 1, 9], [0, 8, 9])
    wins = {
        "big": big,
        "small": not big,
        "odd": odd,
        "even": not odd,
        "big_odd": big and odd,
        "big_even": big and not odd,
        "small_odd": not big and odd,
        "small_even": not big and not odd,
        "extreme_big": total >= 22,
        "extreme_small": total <= 5,
        "dragon": total % 3 == 0,
        "tiger": total % 3 == 1,
        "leopard_type": total % 3 == 2,
        "triple": triple,
        "pair": pair,
        "straight": straight,
    }
    if play in NEW_ODDS:
        if play.startswith("pos_"):
            _, position, selection = play.split("_", 2)
            digit = balls["abc".index(position)]
            choices = {
                "big": digit >= 5,
                "small": digit <= 4,
                "odd": digit % 2 == 1,
                "even": digit % 2 == 0,
                "big_odd": digit >= 5 and digit % 2 == 1,
                "big_even": digit >= 5 and digit % 2 == 0,
                "small_odd": digit <= 4 and digit % 2 == 1,
                "small_even": digit <= 4 and digit % 2 == 0,
            }
            win = (
                digit == int(selection[7:])
                if selection.startswith("number_")
                else choices[selection]
            )
        else:
            win = {
                "edge_small": total <= 9,
                "edge_big": total >= 18,
                "edge": total <= 9 or total >= 18,
                "middle": 10 <= total <= 17,
            }[play]
        return ("win", amount * room["odds"][play] // 100) if win else ("lose", 0)
    win = total == int(play[7:]) if play.startswith("number_") else wins[play]
    if not win:
        return "lose", 0
    room_id = room["id"]
    special = total in (13, 14)
    refund = play in COMBO and special
    if room_id in {"room27", "room28"}:
        refund = play in COMBO and (special or pair or straight or triple)
    if room_id == "room32":
        refund = play in (BASIC | COMBO) and (special or 0 in balls or 9 in balls)
    if refund:
        return "refund", amount
    multiplier = room["odds"][play]
    if play in BASIC and special:
        if room_id == "room18":
            multiplier = 180
        elif room_id == "special":
            multiplier = 180 if amount > 50000 else 188
        elif room_id == "double":
            multiplier = 160
    return "win", amount * multiplier // 100
