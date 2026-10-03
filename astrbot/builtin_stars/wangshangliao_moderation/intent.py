"""Conservative execution guard over the current authenticated user utterance."""

import re


def allows_mutation(text: str, action: str) -> bool:
    """Require a present, explicit request in addition to the model's tool choice.

    Args:
        text: Original incoming user text, never a tool result or model summary.
        action: Fixed action chosen by the model.

    Returns:
        False for queries, negations, conditions, quotations or unclear requests.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    # These ambiguous forms must be clarified rather than interpreted as consent.
    if re.search(
        r"不要|别再|别把|别禁|别踢|不用|取消|撤销|停止|如果|假如|假设|一旦|要是|是否|有没有|是不是|查一下|查询|解释|介绍|怎么|为什么|能否|能不能|如何|(?:吗|么|[?？])\s*$|他说|她说|对方说|引用|转发|例如|举例|[‘’“”\"`]|\b(?:don't|do not|if|whether|quote|cancel)\b",
        text,
        re.I,
    ):
        return False
    tokens = {
        "mute": r"禁言|闭嘴|mute",
        "unmute": r"解禁|解除禁言|恢复.{0,40}发言|unmute",
        "kick": r"踢出|踢掉|移出|移除|kick",
        "announce": r"发布公告|发公告|公告.{0,8}(?:发|发布)|announce",
        "mute_all": r"全员禁言|全群禁言|mute all",
        "unmute_all": r"解除全员禁言|解除全群禁言|全员解禁|恢复全群发言|unmute all",
        "card_execute": r"执行.{0,20}预览|确认.{0,20}(?:改名|名片|预览)|开始.{0,20}(?:改名|规范)|execute.{0,20}preview",
    }
    if action == "mute" and re.search(r"解禁|解除|全员|全群|unmute", text, re.I):
        return False
    if action == "mute_all" and re.search(r"解除|恢复|unmute", text, re.I):
        return False
    return bool(re.search(tokens.get(action, r"(?!)"), text, re.I))
