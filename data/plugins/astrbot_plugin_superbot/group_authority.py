"""Request-local Telegram authority, with explicit owner-issued designations."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .store import Rejected

CURRENT = ContextVar("superbot_authority", default=None)


class VerificationUnavailable(Rejected):
    """Identity could not be read; deny now without declaring authorization lost."""


@dataclass(frozen=True)
class GroupProof:
    """Authority cannot be inherited by a child task or another group."""

    store: object
    task: object
    uid: str
    chat: str
    status: str
    versions: tuple


def versions(store, chat, db=None):
    """Read the group and ownership revisions for optimistic authorization.

    Args:
        store: Instance store.
        chat: Exact group.
        db: Optional transaction.

    Returns:
        Immutable revision tuple.
    """
    row = (
        (db or store.db)
        .execute(
            "SELECT m.version,g.version,g.tenant,g.status,t.version,t.status "
            "FROM mod_groups m LEFT JOIN tenant_groups g ON g.chat=m.chat "
            "LEFT JOIN tenants t ON t.id=g.tenant WHERE m.chat=?",
            (str(chat),),
        )
        .fetchone()
    )
    if not row:
        raise Rejected("该群尚未接入本BOT。")
    return tuple(row)


def require(store, uid, chat, scope=None, db=None):
    """Reject absent, inherited, stale or cross-group authorization.

    Args:
        store: Instance store.
        uid: Actor.
        chat: Target group.
        scope: Optional local capability.
        db: Optional transaction.

    Returns:
        Current verified proof.
    """
    proof = CURRENT.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if (
        not proof
        or proof.store is not store
        or proof.task is not task
        or (proof.uid, proof.chat) != (str(uid), str(chat))
        or scope not in {None, "moderation", "game", "points", "ads"}
        or proof.versions != versions(store, chat, db)
    ):
        raise Rejected("请重新打开本群管理，核实群主或指定群管身份。")
    return proof


def clear_proof():
    """Clear request state at ingress and teardown."""
    CURRENT.set(None)


def stamp(store, uid, payload):
    """Attach a local authorization epoch to server-owned management state."""
    result = dict(payload)
    proof = CURRENT.get()
    nested = result.get("payload")
    chat = result.get("chat") or (
        nested.get("chat") if isinstance(nested, dict) else None
    )
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if (
        "_group_guard" not in result
        and proof
        and proof.store is store
        and proof.task is task
        and proof.uid == str(uid)
        and proof.chat == str(chat)
    ):
        result["_group_guard"] = [str(chat), list(versions(store, chat))]
    return result


def unstamped(store, payload):
    """Reject a changed epoch without exposing internal context to handlers."""
    result = dict(payload)
    guard = result.pop("_group_guard", None)
    if guard and guard[1] != list(versions(store, guard[0])):
        raise Rejected("本群设置或群管授权已变化，请重新打开。")
    return result


class GroupAuthority:
    """Verify live membership without granting global roles."""

    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        if not getattr(runtime, "tenants", None):
            from .tenants import Tenants

            runtime.tenants = Tenants(runtime)

    @asynccontextmanager
    async def session(self, uid, chat, permission=None, *, allow_paused=False):
        """Authorize one operation using current Telegram state.

        Args:
            uid: Acting user.
            chat: Registered target group.
            permission: Optional bot capability.
            allow_paused: Permit management while business is paused.

        Yields:
            Verified, task-bound proof.
        """
        before = versions(self.store, chat)
        try:
            info = await self.runtime.bot.get_chat(chat)
            actor = await self.runtime.bot.get_chat_member(chat, int(uid))
            bot = await self.runtime.bot.get_chat_member(chat, self.runtime.bot.id)
            if info.type != "supergroup":
                raise Rejected("仅支持已接入的超级群。")
            if before[2] not in {None, "platform"} and not allow_paused:
                binding = self.store.db.execute(
                    "SELECT t.owner FROM tenant_groups g JOIN tenants t ON t.id=g.tenant WHERE g.chat=?",
                    (str(chat),),
                ).fetchone()
                registered = await self.runtime.bot.get_chat_member(
                    chat, int(binding["owner"])
                )
                if registered.status != "creator":
                    self.store.suspend_group(chat, "ownership_or_bot_rights")
                    raise Rejected("群主已变化，原经营归属需核查；旧配置和订单未转移。")
            if actor.status not in {"creator", "administrator"}:
                raise Rejected("无权限：仅当前Telegram群主或群主指定的群管可管理本群。")
            await self.runtime.tenants.verify_designation(uid, chat, actor.status)
            if bot.status not in {"creator", "administrator"}:
                raise Rejected("机器人不在群内或尚未设为管理员。")
            if (
                permission
                and bot.status != "creator"
                and not getattr(bot, permission, False)
            ):
                raise Rejected("机器人缺少权限：" + permission)
        except Rejected:
            raise
        except Exception as exc:
            raise VerificationUnavailable(
                "暂时无法核验群身份或机器人权限，请稍后重试。"
            ) from exc
        if versions(self.store, chat) != before:
            raise Rejected("核验期间群配置或授权已变化，请重新操作。")
        if not allow_paused and (
            before[3] not in {None, "active"}
            or before[5] not in {None, "active"}
            or not self.store.db.execute(
                "SELECT enabled FROM mod_groups WHERE chat=?", (str(chat),)
            ).fetchone()[0]
        ):
            raise Rejected("本群已暂停新业务。")
        token = CURRENT.set(
            GroupProof(
                self.store,
                asyncio.current_task(),
                str(uid),
                str(chat),
                actor.status,
                before,
            )
        )
        try:
            yield CURRENT.get()
        finally:
            CURRENT.reset(token)
