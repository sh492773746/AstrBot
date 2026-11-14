"""Authenticated group command scope and explicit billing confirmation."""

import time
from dataclasses import dataclass

from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError


@dataclass
class Session:
    """Hold one expiring, group-scoped billing consent."""

    expires: float
    confirm: str = ""
    stamp: tuple = ()
    message: str = ""
    preview_message: str = ""
    busy: bool = False
    closed: bool = False


class CommandMenu:
    """Bind group operations without private routing or numbered menus."""

    def __init__(self, service):
        self.service = service
        self.sessions = {}

    def key(self, event):
        """Return an authenticated account, actor and current-group key.

        Args:
            event: Native group message with its verified transport identity.

        Returns:
            Isolation key, or None when a group identity cannot be established.
        """
        if event.is_private_chat() or not event.get_group_id():
            return None
        actor = getattr(event, "actor_identity", None)
        origin = getattr(event, "unified_msg_origin", "")
        if (
            not isinstance(actor, ActorIdentity)
            or not origin
            or actor.account != str(event.platform.account)
            or actor.claimed_uid != str(event.get_sender_id())
        ):
            return None
        try:
            principal = actor.principal
        except (ProtocolError, UnicodeError):
            return None
        return actor.account, principal, str(origin), str(event.get_group_id())

    def stamp(self, event):
        """Freeze current group authority and payer consent revisions.

        Args:
            event: Authenticated native group message.

        Returns:
            Revision tuple invalidating stale or revoked confirmations.
        """
        adapter = event.platform
        group = event.get_group_id()
        tenant = self.service.tenants.get((str(adapter.account), group), {})
        return (
            event.get_sender_id(),
            getattr(event, "policy_epoch", None),
            adapter.config.get("_business_revision"),
            group,
            tenant.get("version"),
            tenant.get("epoch"),
            tenant.get("native_role"),
            (str(adapter.account), group) in self.service.suspended_groups,
            bool(self.service.membership_receiving.get((str(adapter.account), group))),
            self.service.settings["version"],
            self.service.group_ai.configs.get((str(adapter.account), group), {}).get(
                "version", 0
            ),
            self.service.plans.confirm_stamp(
                str(adapter.account),
                group,
                event.actor_identity.principal,
                event.actor_identity.verified_uid,
            ),
        )

    def dismiss(self, event):
        """Cancel unsubmitted group confirmations and member-list drafts.

        Args:
            event: Authenticated cancellation message.
        """
        for name in ("member_whitelist", "member_blacklist"):
            owner = getattr(self.service, name, None)
            if owner is not None:
                owner.cancel(event)
        state = self.sessions.pop(self.key(event), None)
        if state is not None:
            state.closed = True
            state.confirm = ""

    def accepts(self, event, text):
        """Claim only explicit cancellation or an existing consent response.

        Args:
            event: Authenticated group message.
            text: Actual message text, not a model-selected operation.

        Returns:
            Whether a bounded group confirmation owns the input.
        """
        key = self.key(event)
        if key is None:
            return False
        if text in {"取消", "0"}:
            return True
        state = self.sessions.get(key)
        return bool(
            state
            and not state.closed
            and state.confirm == "确认本群扣费"
            and state.expires > time.monotonic()
            and text == "1"
        )

    async def handle(self, event, text):
        """Commit one separately confirmed group consent through existing guards.

        Args:
            event: Fresh authenticated group message.
            text: Actual user input.

        Returns:
            Whether a confirmation or cancellation consumed the message.
        """
        if not self.accepts(event, text):
            return False
        event.set_extra("_component_claimed", True)
        event.set_extra("wsl_plain_command", True)
        if text in {"取消", "0"}:
            self.dismiss(event)
            if self.service.admit_command(event):
                await event.send(event.plain_result("已取消尚未提交的操作。"))
            return True
        key = self.key(event)
        state = self.sessions.get(key)
        message = str(event.message_obj.message_id)
        if state is None or state.busy or message == state.preview_message:
            return True
        if not self.service.admit_command(event):
            return True
        if self.stamp(event) != state.stamp:
            self.dismiss(event)
            await event.send(event.plain_result("本群设置或授权已变化，本次未确认。"))
            return True
        state.busy, state.closed = True, True
        self.sessions.pop(key, None)
        event.set_extra("_wsl_billing_consent_stamp", state.stamp)
        reply = await self.service.manager_command(event, state.confirm)
        if isinstance(reply, str):
            await event.send(event.plain_result(reply))
        return True
