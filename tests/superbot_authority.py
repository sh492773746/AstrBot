"""Offline fixtures use the production owner-designation workflow."""

from data.plugins.astrbot_plugin_superbot.group_authority import GroupAuthority


async def designate_admin(runtime, uid, *, owner="1", chat="-1001", enabled=True):
    """Apply a current owner's designation against mocked Telegram membership.

    Args:
        runtime: Test runtime containing its real store and Telegram substitute.
        uid: Administrator being designated or revoked.
        owner: Current Telegram creator.
        chat: Exact registered group.
        enabled: Whether to grant or revoke the designation.
    """
    authority = GroupAuthority(runtime)
    version = runtime.store.db.execute(
        "SELECT version FROM mod_groups WHERE chat=?", (chat,)
    ).fetchone()[0]
    await authority.runtime.tenants.designate(owner, chat, uid, enabled, version)
