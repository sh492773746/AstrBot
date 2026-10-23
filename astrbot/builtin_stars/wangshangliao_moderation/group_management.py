"""Dashboard-owned group grants, separate from cashback and framework admins."""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

from astrbot.core.platform.sources.wangshangliao.database import (
    AsyncDatabase,
    account_guard,
)
from astrbot.core.platform.sources.wangshangliao.identity import ActorIdentity
from astrbot.core.platform.sources.wangshangliao.wire import ProtocolError
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

MANAGER_COMMANDS = frozenset(
    {
        "帮助",
        "我的权限",
        "群列表",
        "选择群",
        "成员列表",
        "禁言名单",
        "成员搜索",
        "下一页",
        "能力",
        "规则",
        "违规计数",
        "结果",
        "排名",
        "禁言",
        "解禁",
        "公告",
        "全员禁言",
        "解除全员禁言",
        "踢出",
        "确认踢出",
        "定时禁言",
        "确认定时",
        "定时状态",
        "暂停定时",
        "恢复定时",
        "删除定时",
    }
)


def native_manager_ids(members, account):
    """Select verified native administrators, excluding the acting bot.

    Args:
        members: Member rows from a complete authenticated business directory.
        account: Current bot's business account.

    Returns:
        Business IDs eligible to receive a group management grant.
    """
    return {
        str(row["userId"])
        for row in members
        if row.get("groupRole") in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}
        and row.get("nimId")
        and str(row.get("userId", "")) != str(account)
    }


def management_command(text):
    """Parse an exact private command envelope, never natural language.

    Args:
        text: Authenticated incoming text.

    Returns:
        Normalized allowlisted command or an empty string.
    """
    from .syntax import recognize_command

    if (
        not isinstance(text, str)
        or len(text) > 4096
        or not text.startswith(("管理 ", "/管理 "))
    ):
        return ""
    inner = text.split(" ", 1)[1]
    if inner.startswith(("管理 ", "/管理 ")):
        return ""
    command = recognize_command(inner)
    if command and command.split(" ", 1)[0] in MANAGER_COMMANDS:
        return command
    return ""


class GroupManagement:
    """Cache committed grants bound to instance, authenticated account and group."""

    def __init__(self, context, root=None):
        self.context = context
        self.path = (
            Path(root)
            if root is not None
            else Path(get_astrbot_data_path())
            / "plugin_data"
            / "wangshangliao_moderation"
        ) / "group_management.sqlite3"
        self.assignments = {}
        self.versions = {}
        self.lock = asyncio.Lock()

    def database(self):
        """Open the independent low-write grant store.

        Returns:
            Connection containing grants and append-only administrator audit.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=5)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS grants(
                instance TEXT,account TEXT,group_id TEXT,version INTEGER,
                users TEXT NOT NULL,PRIMARY KEY(instance,account,group_id));
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY,created REAL,actor TEXT,instance TEXT,
                account TEXT,group_id TEXT,before_json TEXT,after_json TEXT);
        """)
        self.path.chmod(0o600)
        return db

    async def load(self):
        """Restore committed grants without touching existing business policies."""
        async with self.lock:
            async with AsyncDatabase(self.database) as db:
                rows = await db.fetchall(
                    "SELECT instance,account,group_id,version,users FROM grants"
                )
            self.assignments = {
                (i, a, g): tuple(json.loads(users)) for i, a, g, _, users in rows
            }
            self.versions = {(i, a, g): version for i, a, g, version, _ in rows}

    def key(self, adapter, group):
        """Return the account-scoped grant identity.

        Args:
            adapter: Current authenticated native adapter.
            group: Business group ID.

        Returns:
            Immutable grant key.
        """
        return (str(adapter.config["id"]), str(adapter.account), str(group))

    def superadmin(self, event):
        """Recheck the existing effective AstrBot admin list.

        Args:
            event: Native authenticated event.

        Returns:
            Whether the sender retains total-control command authority.
        """
        config = event.platform.config
        identity = getattr(event, "actor_identity", None)
        return (
            isinstance(identity, ActorIdentity)
            and bool(identity.peer)
            and identity.account == str(event.platform.account)
            and identity.claimed_uid == str(event.get_sender_id())
            and identity.verified_uid == identity.claimed_uid
            and config.get("_business_active") is not False
            and config.get("enable", True)
            and getattr(event, "policy_epoch", config.get("_business_epoch"))
            == config.get("_business_epoch")
            and event.is_admin()
            and event.get_extra("_api_key_allow_admin_role") is not False
            and str(event.get_sender_id())
            in {
                str(uid)
                for uid in self.context.get_config(event.unified_msg_origin).get(
                    "admins_id", []
                )
            }
        )

    def groups(self, event):
        """Filter enabled groups without changing framework roles.

        Args:
            event: Native authenticated event.

        Returns:
            Only groups the sender may manage on this account.
        """
        adapter = event.platform
        identity = getattr(event, "actor_identity", None)
        if (
            not isinstance(identity, ActorIdentity)
            or not identity.peer
            or identity.account != str(adapter.account)
            or identity.claimed_uid != str(event.get_sender_id())
            or identity.verified_uid != identity.claimed_uid
            or adapter.config.get("_business_active") is False
            or not adapter.config.get("enable", True)
            or getattr(event, "policy_epoch", adapter.config.get("_business_epoch"))
            != adapter.config.get("_business_epoch")
            or event.get_extra("_api_key_allow_admin_role") is False
        ):
            return []
        groups = adapter.config.get("enabled_groups", [])
        if self.superadmin(event):
            return list(groups)
        return [
            g
            for g in groups
            if str(event.get_sender_id())
            in self.assignments.get(self.key(adapter, g), ())
        ]

    def allowed(self, event, group=""):
        """Check exact command scope; no chat text can grant a role.

        Args:
            event: Native event whose sender is assigned by the dashboard.
            group: Optional exact target group.

        Returns:
            Whether the sender may enter or act within the selected scope.
        """
        groups = self.groups(event)
        return group in groups if group else bool(groups) or self.superadmin(event)

    async def save(self, adapter, group, users, expected, actor, guard):
        """Commit a dashboard assignment with account and revision rechecks.

        Args:
            adapter: Authenticated account receiving the group grant.
            group: Exact enabled group.
            users: Verified current native administrator IDs.
            expected: Last seen group grant version.
            actor: Authenticated dashboard principal.
            guard: Recheck dashboard lifecycle and saved bot identity.

        Returns:
            New committed group version.

        Raises:
            ProtocolError: If identity, group, membership or revision changed.
        """
        if (
            not isinstance(users, list)
            or len(users) > 20
            or any(
                not isinstance(u, str)
                or not u.isascii()
                or not u.isdecimal()
                or not 0 < int(u) < 1 << 30
                for u in users
            )
            or len(set(users)) != len(users)
            or type(expected) is not int
            or expected < 0
        ):
            raise ProtocolError("group_management_arguments")
        key = self.key(adapter, group)
        check = account_guard(adapter, guard)
        async with self.lock:
            check()
            roster = await adapter.get_moderation_members(group)
            check()
            members = native_manager_ids(
                roster.get("groupMemberInfo", []), adapter.account
            )
            if not roster.get("complete") or any(
                u not in members or u == str(adapter.account) for u in users
            ):
                raise ProtocolError("group_management_members")
            # A cancelled COMMIT may already be durable. Keep authority absent
            # until the drained transaction has been reconciled with disk.
            self.assignments.pop(key, None)
            self.versions.pop(key, None)
            try:
                async with AsyncDatabase(self.database, write=True, guard=check) as db:
                    row = await db.fetchone(
                        "SELECT version,users FROM grants WHERE instance=? AND account=? AND group_id=?",
                        key,
                    )
                    version, before = row if row else (0, "[]")
                    if version != expected:
                        raise ProtocolError("group_management_changed")
                    encoded = json.dumps(sorted(users))
                    await db.execute(
                        "INSERT OR REPLACE INTO grants VALUES(?,?,?,?,?)",
                        (*key, version + 1, encoded),
                    )
                    await db.execute(
                        "INSERT INTO audit VALUES(NULL,?,?,?,?,?,?,?)",
                        (time.time(), actor, *key, before, encoded),
                    )
            except BaseException:

                async def reconcile():
                    async with AsyncDatabase(self.database) as db:
                        return await db.fetchone(
                            "SELECT version,users FROM grants WHERE instance=? AND account=? AND group_id=?",
                            key,
                        )

                restoring = asyncio.create_task(reconcile())
                while not restoring.done():
                    try:
                        await asyncio.shield(restoring)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not restoring.cancelled() and restoring.exception() is None:
                    restored = restoring.result()
                    if restored:
                        self.versions[key] = restored[0]
                        self.assignments[key] = tuple(json.loads(restored[1]))
                raise
            self.assignments[key] = tuple(sorted(users))
            self.versions[key] = version + 1
            return version + 1
