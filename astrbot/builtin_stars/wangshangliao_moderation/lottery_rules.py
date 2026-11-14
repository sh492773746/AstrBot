"""Validate frozen lottery entry phrases and exact, cent-based packet plans."""

import secrets
import unicodedata

from .syntax import recognize_command

DEFAULT_ENTRY_PHRASE = "参与玩家福利抽奖"
PACKET_LABELS = {
    "ordinary": "普通红包",
    "lucky": "拼手气",
    "tiered": "分档拼手气",
}


def validate_luckiest_bonus(value, mode):
    """Validate the separate, optional award without changing the packet pool.

    Args:
        value: Extra budget in integer cents, with zero disabling the award.
        mode: Frozen or proposed packet allocation mode.

    Returns:
        The validated extra budget in cents.

    Raises:
        ValueError: If the amount or packet mode is incompatible.
    """
    if type(value) is not int or not 0 <= value <= 99999999:
        raise ValueError("人气王额外奖励须为0至999999.99元，最多两位小数")
    if value and mode != "lucky":
        raise ValueError("人气王额外奖励仅支持拼手气红包；其他类型请将奖励设为0")
    return value


def validate_entry_phrase(value):
    """Validate an exact public phrase without shadowing management commands.

    Args:
        value: Proposed single-line phrase.

    Returns:
        The validated phrase, without normalization.

    Raises:
        ValueError: If the phrase is unsafe or conflicts with a command.
    """
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32
        or value != value.strip()
        or value.startswith(("/", "@"))
        or value in {"开通", "取消"}
        or value.startswith("认领 ")
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(char in value for char in ("\n", "\r", "\u2028", "\u2029"))
        or (
            value not in {DEFAULT_ENTRY_PHRASE, "参与抽奖"} and recognize_command(value)
        )
        or value.isdecimal()
    ):
        raise ValueError(
            "报名口令须为1至32字单行文字，不能使用现有命令、纯数字或以 /、@ 开头"
        )
    return value


def validate_reserved_members(value, tiers):
    """Validate optional member-to-tier reservations for a tiered packet draw.

    Args:
        value: Frozen reservation rows containing member, peer, name and amount.
        tiers: Validated fixed packet tiers in integer cents.

    Returns:
        The unchanged reservation list after structural validation.

    Raises:
        ValueError: If identities are duplicated or a tier is over-reserved.
    """
    if type(value) is not list or len(value) > 100:
        raise ValueError("指定档位成员最多100人")
    tier_counts = {
        tier["amount"]: tier["count"]
        for tier in tiers
        if type(tier) is dict
        and type(tier.get("amount")) is int
        and type(tier.get("count")) is int
    }
    used_counts = {}
    members, peers = set(), set()
    for row in value:
        if (
            type(row) is not dict
            or set(row) != {"member", "peer", "name", "amount"}
            or not isinstance(row["member"], str)
            or not row["member"].isascii()
            or not row["member"].isdigit()
            or not 1 <= len(row["member"]) <= 20
            or int(row["member"]) <= 0
            or not isinstance(row["peer"], str)
            or not row["peer"].isascii()
            or not row["peer"].isdigit()
            or not 1 <= len(row["peer"]) <= 20
            or int(row["peer"]) <= 0
            or not isinstance(row["name"], str)
            or len(row["name"]) > 128
            or any(not char.isprintable() for char in row["name"])
            or type(row["amount"]) is not int
            or row["amount"] not in tier_counts
            or row["member"] in members
            or row["peer"] in peers
        ):
            raise ValueError("指定成员须来自当前群，且同一成员只能指定一次")
        members.add(row["member"])
        peers.add(row["peer"])
        used_counts[row["amount"]] = used_counts.get(row["amount"], 0) + 1
        if used_counts[row["amount"]] > tier_counts[row["amount"]]:
            raise ValueError("指定成员数量不能超过对应固定档位份数")
    return value


def packet_plan(total, count, mode, tiers, *, generate=True, integer_packets=False):
    """Validate and optionally generate an exact packet plan in integer cents.

    Args:
        total: Total budget in cents.
        count: Number of packets, including fixed tiers.
        mode: Ordinary, lucky or tiered allocation.
        tiers: Fixed tiers containing integer amount and count fields.
        generate: Generate random allocations only when opening a new draw.
        integer_packets: Allocate whole yuan rather than individual cents.

    Returns:
        A complete positive packet plan, or an empty list for validation only.

    Raises:
        ValueError: If the budget, quantities or fixed tiers are inconsistent.
    """
    if (
        type(total) is not int
        or type(count) is not int
        or not 1 <= count <= 100
        or not count <= total <= 99999999
        or mode not in PACKET_LABELS
        or type(tiers) is not list
        or len(tiers) > 10
        or (mode == "tiered") != bool(tiers)
    ):
        raise ValueError("红包须为1至100份且每份至少0.01元；分档拼手气须设置1至10档")
    if type(integer_packets) is not bool:
        raise ValueError("整数元开关须为布尔值")
    if integer_packets and (total % 100 or total < count * 100):
        raise ValueError("整数元红包总金额须为整元，且每份至少1元")
    fixed = []
    amounts = set()
    for tier in tiers:
        if (
            type(tier) is not dict
            or set(tier) != {"amount", "count"}
            or type(tier["amount"]) is not int
            or not 1 <= tier["amount"] <= 99999999
            or type(tier["count"]) is not int
            or not 1 <= tier["count"] <= 100
            or tier["amount"] in amounts
        ):
            raise ValueError("每档须设置有效金额和1至100份，相同金额请合并份数")
        if integer_packets and tier["amount"] % 100:
            raise ValueError("整数元红包的固定档位须为整元")
        amounts.add(tier["amount"])
        fixed.extend([tier["amount"]] * tier["count"])
    remaining_count, remaining_total = count - len(fixed), total - sum(fixed)
    if (
        remaining_count < 0
        or remaining_total < remaining_count * (100 if integer_packets else 1)
        or (remaining_count == 0 and remaining_total != 0)
    ):
        raise ValueError(
            "固定档位超出总份数或总金额；剩余每份须至少0.01元，满份时金额须恰好用完"
        )
    if not generate:
        return []
    unit = 100 if integer_packets else 1
    total //= unit
    remaining_total //= unit
    fixed = [amount // unit for amount in fixed]
    rng = secrets.SystemRandom()
    if mode == "ordinary":
        share, remainder = divmod(total, count)
        return [(share + (index < remainder)) * unit for index in range(count)]
    if remaining_count:
        cuts = [
            0,
            *sorted(rng.sample(range(1, remaining_total), remaining_count - 1)),
            remaining_total,
        ]
        fixed.extend(right - left for left, right in zip(cuts, cuts[1:]))
    if mode == "tiered":
        rng.shuffle(fixed)
    return [amount * unit for amount in fixed]
