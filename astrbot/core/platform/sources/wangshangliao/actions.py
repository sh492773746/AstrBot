"""Versioned in-process actions between Wangshangliao business and transport."""

import asyncio
import inspect
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from .blacklist import (
    BLACKLIST_MUTE_MINUTES,
    BlacklistIntent,
    authorize_intent,
    protected_target,
)
from .database import account_guard
from .group_scope import group_revision
from .policy import authorize_action
from .wire import ProtocolError

ACTION_VERSION = 1
AUTOMATIC_KICK_PREFIXES = ("automatic-kick/", "message/", "content/", "progressive/")
ACTIONS = frozenset(
    {
        "mute",
        "unmute",
        "kick",
        "announce",
        "mute_all",
        "unmute_all",
        "rename",
        "cleanup",
        "recall",
        "warning",
    }
)


@dataclass(frozen=True)
class ActionRequest:
    """Immutable action intent bound to one adapter business generation.

    Args:
        version: Supported contract version, never silently downgraded.
        instance: Original instance identifier.
        account: Original authenticated account identifier.
        epoch: Original runtime business generation.
        revision: Original business policy revision.
        operation: Existing persistent idempotency identifier.
        action: Fixed action name.
        group: Positive business group identifier.
        member: Positive target identifier for member actions, otherwise zero.
        text: Announcement or proposed card text.
        minutes: Member mute duration.
        expected_card: Original card captured by the preview.
        expected_nim: Original transport identity captured by the preview.
        message: Stored message identifier for violation recall.
        mention_name: Original leading label, resolved to a member nickname on send.
        group_revision: Original group-specific policy revision.
        blacklist_intent: Stored canonical-join capability, never a prefix grant.
    """

    version: int
    instance: str
    account: str
    epoch: str | int | None
    revision: str | int | None
    operation: str
    action: str
    group: int
    member: int = 0
    text: str = ""
    minutes: int = 1
    expected_card: str | None = None
    expected_nim: str | None = None
    message: str = ""
    mention_name: str | None = None
    group_revision: str | int | None = None
    blacklist_intent: BlacklistIntent | None = None
    blacklist_fixture: object | None = None
    blacklist_guard: object | None = None

    @classmethod
    def bind(cls, adapter, operation, action, group, **parameters):
        """Capture routing identity before awaiting transport execution.

        Args:
            adapter: Original live adapter.
            operation: Stable persisted idempotency identifier.
            action: Fixed contract action.
            group: Business group identifier.
            parameters: Action-specific dataclass fields.

        Returns:
            Immutable request bound to the current account and policy generation.
        """
        return cls(
            version=ACTION_VERSION,
            instance=adapter.config["id"],
            account=adapter.account,
            epoch=adapter.config.get("_business_epoch"),
            revision=adapter.config.get("_business_revision"),
            group_revision=group_revision(adapter.config, str(group)),
            operation=operation,
            action=action,
            group=group,
            **parameters,
        )


class ActionPort(Protocol):
    """Business-facing transport boundary; no network deserialization is implied."""

    async def execute_action(self, request: ActionRequest, *, authorize=None) -> dict:
        """Execute a versioned intent with optional business reauthorization.

        Args:
            request: Immutable account-bound action.
            authorize: Trusted in-process callback, not a user-supplied grant.

        Returns:
            Versioned receipt retaining the existing operation evidence.
        """
        ...


async def dispatch(adapter, request, *, authorize=None):
    """Validate and route one action without bypassing existing native checks.

    Args:
        adapter: Original native transport instance.
        request: Versioned action request.
        authorize: Optional synchronous or asynchronous business check.

    Returns:
        Existing receipt plus authoritative version and routing identity.

    Raises:
        ProtocolError: On unsupported versions, malformed parameters or stale scope.
    """
    if type(request) is not ActionRequest:
        raise ProtocolError("action_request_invalid")
    if type(request.version) is not int or request.version != ACTION_VERSION:
        raise ProtocolError("action_version_unsupported")
    if (
        not isinstance(request.action, str)
        or request.action not in ACTIONS
        or not isinstance(request.operation, str)
        or not request.operation.strip()
        or len(request.operation.encode()) > 512
        or any(ord(char) < 32 for char in request.operation)
        or type(request.group) is not int
        or not 0 < request.group < 1 << 63
        or type(request.member) is not int
        or not isinstance(request.text, str)
        or type(request.minutes) is not int
        or not (
            1 <= request.minutes <= 1440
            or 1 <= request.minutes <= BLACKLIST_MUTE_MINUTES
            and request.action == "mute"
            and request.blacklist_intent is not None
        )
    ):
        raise ProtocolError("action_parameters_invalid")
    member_action = request.action in {
        "mute",
        "unmute",
        "kick",
        "rename",
        "cleanup",
        "recall",
        "warning",
    }
    if (member_action and not 0 < request.member < 1 << 63) or (
        not member_action and request.member != 0
    ):
        raise ProtocolError("action_target_invalid")
    if request.action in {"rename", "cleanup", "recall", "warning"} and (
        not isinstance(request.expected_nim, str) or not request.expected_nim
    ):
        raise ProtocolError("action_identity_required")
    if request.action == "rename" and (
        not isinstance(request.expected_card, str)
        or not request.text.strip()
        or len(request.text.encode()) > 256
    ):
        raise ProtocolError("action_card_invalid")
    if (
        not isinstance(request.message, str)
        or (
            request.action == "recall"
            and (
                not request.message
                or len(request.message.encode()) > 128
                or request.operation
                != f"recall/{request.account}/{request.group}/{request.message}"
            )
        )
        or (request.action != "recall" and request.message)
    ):
        raise ProtocolError("action_message_invalid")
    if request.action == "warning" and (
        not request.text.strip()
        or len(request.text.encode()) > 4096
        or (
            request.mention_name is not None
            and (
                not isinstance(request.mention_name, str)
                or not request.mention_name
                or not request.text.startswith(f"@{request.mention_name} ")
                or not request.expected_nim.isascii()
                or not request.expected_nim.isdigit()
                or not 0 < int(request.expected_nim) < 1 << 32
            )
        )
    ):
        raise ProtocolError("action_warning_invalid")
    if request.action != "warning" and request.mention_name is not None:
        raise ProtocolError("action_warning_invalid")
    blacklist_action = (
        request.operation.startswith("blacklist/")
        or request.blacklist_intent is not None
    )
    if blacklist_action and (
        type(request.blacklist_intent) is not BlacklistIntent
        or request.expected_nim != request.blacklist_intent.peer
    ):
        raise ProtocolError("blacklist_intent_invalid")
    if request.action == "kick" and request.operation.startswith(
        AUTOMATIC_KICK_PREFIXES
    ):
        raise ProtocolError("automatic_kick_not_authorized")
    scheduled = request.operation.startswith("schedule/")
    if scheduled and (
        request.action not in {"mute_all", "unmute_all"} or authorize is None
    ):
        raise ProtocolError("action_schedule_authority_required")
    if authorize is not None and not callable(authorize):
        raise ProtocolError("action_authority_invalid")
    guard = account_guard(adapter)

    async def check(*, invoke_authorize=True):
        guard()
        config = adapter.config
        if (
            not isinstance(request.instance, str)
            or not request.instance
            or not isinstance(request.account, str)
            or not request.account
            or (request.instance, request.account, request.epoch, request.revision)
            != (
                config["id"],
                adapter.account,
                config.get("_business_epoch"),
                config.get("_business_revision"),
            )
            or config.get("_business_active") is False
            or not config.get("enable", True)
            or request.group_revision != group_revision(config, str(request.group))
        ):
            raise ProtocolError("action_scope_changed")
        if invoke_authorize and authorize is not None:
            checked = authorize()
            if inspect.isawaitable(checked):
                await checked
        guard()
        if request.group_revision != group_revision(adapter.config, str(request.group)):
            raise ProtocolError("action_scope_changed")
        if blacklist_action:
            await asyncio.to_thread(
                authorize_intent,
                request.instance,
                request.account,
                request.operation,
                request.action,
                request.group,
                request.member,
                request.minutes,
                request.blacklist_intent,
            )
            guard()
            if (
                request.member == int(request.account)
                or await asyncio.to_thread(
                    protected_target,
                    adapter,
                    str(request.group),
                    str(request.member),
                    request.expected_nim,
                    fixture=request.blacklist_fixture,
                    intent=request.blacklist_intent,
                )
                or adapter.members.get(str(request.group), {}).get(str(request.member))
                != request.expected_nim
            ):
                raise ProtocolError("blacklist_target_changed")
        if request.action in {"recall", "warning"}:
            authorize_action(
                adapter.config,
                str(request.group),
                "recall" if request.action == "recall" else "mute",
            )
            if (
                not adapter.config.get("moderation", {}).get("automation_enabled")
                or adapter.members.get(str(request.group), {}).get(str(request.member))
                != request.expected_nim
            ):
                raise ProtocolError("action_automation_changed")

    # Schedule callbacks reserve executions only while the native group lock is held.
    await check(invoke_authorize=not scheduled)
    if request.action == "recall":
        status = await adapter.recall_violation(
            str(request.group), request.message, str(request.member), authorize=check
        )
        result = {"status": status}
    elif request.action == "warning":
        text = request.text
        mentions = None
        if request.mention_name is not None:
            roster = await adapter.get_moderation_members(str(request.group))
            await check()
            matches = [
                member
                for member in roster.get("groupMemberInfo", [])
                if str(member.get("userId")) == str(request.member)
            ]
            if (
                not roster.get("complete")
                or len(matches) != 1
                or str(matches[0].get("nimId")) != request.expected_nim
            ):
                raise ProtocolError("action_warning_identity")
            # Login names and routing IDs must never become mention labels.
            nick = ""
            for field in (
                "userNick",
                "groupMemberNick",
                "nick",
                "groupNick",
                "cardName",
                "name",
            ):
                value = matches[0].get(field)
                if not isinstance(value, str):
                    continue
                value = " ".join(
                    "".join(
                        char
                        for char in value
                        if not unicodedata.category(char).startswith("C")
                    ).split()
                )[:100]
                if value and value not in {str(request.member), request.expected_nim}:
                    nick = value
                    break
            text = text[len(f"@{request.mention_name} ") :]
            if nick:
                prefix = f"@{nick} "
                text = prefix + text
                mentions = [
                    {
                        "uid": int(request.expected_nim),
                        "nick": nick,
                        "start": 0,
                        "end": len(prefix.encode("utf-16-le")) // 2,
                    }
                ]
        status = await adapter.send_text(
            str(request.group),
            request.operation,
            text,
            mentions=mentions,
            auto_recall=True,
            authorize=check,
        )
        result = {"status": status}
    elif request.action == "rename":
        result = await adapter.rename_member(
            request.operation,
            request.group,
            request.member,
            request.text,
            request.expected_card,
            request.expected_nim,
            authorize=check,
        )
    elif request.action == "cleanup":
        result = await adapter.cleanup_member(
            request.operation,
            request.group,
            request.member,
            request.expected_nim,
            authorize=check,
        )
    elif scheduled:
        result = await adapter.execute_moderation(
            request.operation, request.action, request.group, scheduled_check=check
        )
    elif blacklist_action:
        result = await adapter.execute_moderation(
            request.operation,
            request.action,
            request.group,
            request.member,
            request.text,
            minutes=request.minutes,
            action_check=check,
            blacklist_intent=request.blacklist_intent,
            blacklist_fixture=request.blacklist_fixture,
            blacklist_guard=request.blacklist_guard,
        )
    else:
        result = await adapter.execute_moderation(
            request.operation,
            request.action,
            request.group,
            request.member,
            request.text,
            minutes=request.minutes,
            action_check=check,
        )
    # A completed upstream request must retain its receipt even after revocation.
    result = dict(result) if isinstance(result, dict) else {}
    status = result.get("status")
    if not isinstance(status, str) or status not in {
        "accepted",
        "verified",
        "rejected",
        "unknown",
    }:
        status = "unknown"
    return {
        **result,
        "version": ACTION_VERSION,
        "instance": request.instance,
        "account": request.account,
        "operation": request.operation,
        "action": request.action,
        "status": status,
    }
