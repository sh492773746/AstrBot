"""Shared group-game navigation with request-scoped administrator checks."""

from functools import wraps

from .store import Rejected
from .tenants import group_admin, platform_group


def scoped_game_page(handler):
    """Wrap four game pages with local rights and legacy-button routing.

    Args:
        handler: Existing game administration handler.

    Returns:
        A private, freshly authorized game handler.
    """

    @wraps(handler)
    async def checked(ui, update, payload):
        from .tenant_ui import action

        if update.effective_chat.type != "private":
            raise Rejected("请私聊打开玩法设置。")
        uid = str(update.effective_user.id)
        name = payload["action"]
        if name.endswith("_groups") and not ui.store.allowed(uid, "game"):
            if not getattr(ui.runtime, "tenants", None) or not getattr(
                ui.runtime, "merchant_ads", None
            ):
                raise Rejected("没有可管理的玩法群。")
            return await action(ui, update, {"action": "tenant_home"})
        chat = payload.get("chat")
        if chat and platform_group(ui.store, chat):
            async with group_admin(ui.runtime, uid, str(chat), allow_inactive=True):
                return await handler(ui, update, payload)
        if chat and not platform_group(ui.store, chat):
            async with group_admin(ui.runtime, uid, str(chat), allow_inactive=True):
                await ui.runtime.tenants.verify(
                    uid, str(chat), "game", allow_paused=True
                )
                if name in {
                    "slots_preview",
                    "slots_save",
                    "mines_preview",
                    "mines_save",
                    "k3_preview",
                    "k3_save",
                } or (
                    name in {"wheel_preview", "wheel_save"}
                    and payload.get("field") == "enabled"
                ):
                    return await action(
                        ui, update, {"action": "tenant_switches", "chat": chat}
                    )
                return await handler(ui, update, payload)
        return await handler(ui, update, payload)

    return checked


def game_back(ui, chat, game):
    """Return one parent and one group-selection route without duplicate overviews.

    Args:
        ui: Current UI.
        chat: Explicit group.
        game: Current game key.

    Returns:
        Navigation button pairs for this group type.
    """
    if not platform_group(ui.store, chat):
        return [
            ("返回玩法设置", {"action": "tenant_games", "chat": chat}),
            ("返回选群", {"action": "tenant_home"}),
        ]
    return [("返回选群", {"action": game + "_groups"})]
