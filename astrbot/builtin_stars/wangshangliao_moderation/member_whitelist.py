"""Native-member search, selection and confirmation for scoped speech exemptions."""

import asyncio
import secrets
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .custom_rules import normalize
from .member_exemptions import read_exemptions, set_exemption
from .open_store import OpenError

TOKEN = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16,64}$")]


class Scope(BaseModel):
    """Keep every request tied to an exact versioned group."""

    model_config = ConfigDict(extra="forbid", strict=True)
    account: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    tenant_version: int = Field(ge=1)


class Read(Scope):
    operation: Literal["read"]


class Search(Scope):
    operation: Literal["search"]
    mode: Literal["add", "remove"]
    query: str = Field(min_length=1, max_length=64)


class Preview(Scope):
    operation: Literal["preview"]
    selection: TOKEN
    index: int = Field(ge=1, le=20)


class Commit(Scope):
    operation: Literal["commit"]
    token: TOKEN


REQUEST = TypeAdapter(
    Annotated[Read | Search | Preview | Commit, Field(discriminator="operation")]
)


def member_name(row):
    """Render a native name without control characters or forged reply lines.

    Args:
        row: Verified native member record.

    Returns:
        Bounded single-line display name.
    """
    name = str(row.get("groupMemberNick") or row.get("userNick") or "群成员")
    return (
        " ".join("".join(c for c in name if c.isprintable() or c.isspace()).split())[
            :128
        ]
        or "群成员"
    )


class MemberWhitelist:
    """Share one server-resolved member workflow between dashboard and fixed chat."""

    def __init__(self, service):
        self.service = service
        self.pending = {}
        self.chat_search = {}

    def cancel(self, event):
        """Invalidate this conversation's unresolved member selections and previews.

        Args:
            event: Original native conversation being cancelled or reset.
        """
        key = self.service.navigation.key(event)
        self.chat_search.pop(key, None)
        for token, row in list(self.pending.items()):
            if row["session"] == key:
                self.pending.pop(token, None)

    async def run(
        self,
        adapter,
        group,
        operation,
        *,
        actor,
        session,
        event=None,
        tenant_version=None,
        mode="",
        query="",
        selection="",
        index=0,
        token="",
    ):
        """Authorize fresh native membership before each read, selection or commit.

        Args:
            adapter: Existing authenticated collector.
            group: Server-selected current group, never a member-provided scope.
            operation: Fixed read, search, preview or commit operation.
            actor: Verified caller audit identity.
            session: Exact authenticated conversation or dashboard principal.
            event: Native event, absent only for the authenticated dashboard.
            tenant_version: Expected dashboard group version.
            mode: Add or remove, only for name searches.
            query: Literal name search within this group.
            selection: Server-issued search token.
            index: One-based choice from that exact search.
            token: Server-issued immutable preview token.

        Returns:
            Bounded current members, an immutable preview or committed result.

        Raises:
            OpenError: If identity, permission, selection or version changed.
        """
        service = self.service
        account = str(adapter.account)
        tenant = dict(service.tenants.get((account, group), {}))
        if (
            not tenant
            or tenant_version is not None
            and tenant_version != tenant["version"]
            or event is not None
            and not service.management.allowed(event, group)
            or event is None
            and not actor.startswith("dashboard/")
        ):
            raise OpenError("member_whitelist_permission")
        base = service.write_guard(adapter, event)
        members = await service.roster(adapter, group)
        base()
        if service.tenants.get((account, group)) != tenant:
            raise OpenError("tenant_changed")
        authority_guard = service.write_guard(
            adapter,
            event,
            group=group,
            tenant=tenant,
            members=members,
            control=event is None,
        )
        candidate = None

        def guard():
            authority_guard()
            if service.store.tenant(account, group) != tenant:
                raise OpenError("tenant_changed")
            latest = service.member_evidence.get((account, group), {})
            if event is not None and latest.get(event.get_sender_id(), {}).get(
                "groupRole"
            ) not in {"GROUP_ROLE_OWNER", "GROUP_ROLE_ADMIN"}:
                raise OpenError("member_whitelist_permission")
            if candidate is not None and mode == "add":
                live = latest.get(candidate["id"], {})
                if (
                    str(live.get("nimId", "")) != candidate["peer"]
                    or member_name(live) != candidate["name"]
                    or live.get("groupRole")
                    not in {"GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
                    or live.get("accountState")
                    in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
                ):
                    raise OpenError("member_whitelist_changed")

        guard()
        if event is not None and (
            event.actor_identity.verified_uid != event.get_sender_id()
            or str(members.get(event.get_sender_id(), {}).get("nimId"))
            != event.actor_identity.peer
        ):
            raise OpenError("private_identity_unverified")
        current_stamp = (
            adapter.config["id"],
            account,
            group,
            tenant["version"],
            tenant["epoch"],
            service.settings["version"],
            adapter.config.get("_business_epoch"),
            adapter.config.get("_business_revision"),
            service.management.versions.get(service.management.key(adapter, group), 0),
        )
        if event is not None:
            choices = service.plugin.commands.selections.get(
                (
                    id(adapter),
                    adapter.account,
                    event.get_sender_id(),
                    event.unified_msg_origin,
                ),
                {},
            )
            current_stamp += (service.navigation.stamp(event), choices.get("expires"))
        version, entries = await asyncio.to_thread(
            read_exemptions,
            adapter.config["id"],
            account,
            group,
            include_names=True,
            include_conflicts=operation == "read",
        )
        guard()
        eligible = {
            uid: row
            for uid, row in members.items()
            if uid != account
            and row.get("groupRole")
            in {"GROUP_ROLE_MEMBER", "GROUP_ROLE_ADMIN", "GROUP_ROLE_OWNER"}
            and row.get("accountState")
            not in {"ACCOUNT_STATE_BAN", "ACCOUNT_STATUS_CANCELLED"}
            and str(row.get("nimId", "")).isascii()
            and str(row.get("nimId", "")).isdigit()
        }
        visible = []
        for uid, entry in entries.items():
            live = eligible.get(uid, {})
            name = (
                member_name(live)
                if str(live.get("nimId", "")) == entry["peer"]
                else entry["name"] or "已离群成员"
            )
            visible.append(
                {
                    "id": uid,
                    "name": name,
                    **({"conflict": True} if entry.get("conflict") else {}),
                }
            )
        if operation == "read":
            return {"version": version, "members": visible}
        now = time.monotonic()
        self.pending = {
            key: row for key, row in self.pending.items() if row["expires"] > now
        }
        self.chat_search = {
            key: value
            for key, value in self.chat_search.items()
            if value in self.pending
        }
        if operation == "search":
            if (
                mode not in {"add", "remove"}
                or not isinstance(query, str)
                or not 1 <= len(query) <= 64
                or not normalize(query)
                or any(not char.isprintable() for char in query)
            ):
                raise OpenError("member_whitelist_query")
            candidates = []
            source = eligible if mode == "add" else entries
            if mode == "remove" and query in source:
                source = {query: source[query]}
            for uid, row in source.items():
                if mode == "add" and uid in entries:
                    continue
                live = eligible.get(uid, {})
                peer = str(row["nimId"]) if mode == "add" else row["peer"]
                names = (
                    [
                        str(live.get(field) or "")
                        for field in ("groupMemberNick", "userNick")
                    ]
                    if str(live.get("nimId", "")) == peer
                    else []
                )
                if mode == "remove":
                    names.append(row["name"])
                if any(normalize(query) in normalize(name) for name in names) or (
                    mode == "remove" and query == uid
                ):
                    candidates.append(
                        {
                            "id": uid,
                            "peer": peer,
                            "name": (
                                member_name(live)
                                if str(live.get("nimId", "")) == peer
                                else row["name"] or "已离群成员"
                            ),
                        }
                    )
            if len(candidates) > 20:
                raise OpenError("member_whitelist_narrow")
            if len(self.pending) >= 4096:
                raise OpenError("service_busy")
            # A newer search replaces this conversation's older choices and drafts.
            for key, row in list(self.pending.items()):
                if row["session"] == session:
                    self.pending.pop(key, None)
            selection = secrets.token_urlsafe(18)
            if candidates:
                self.pending[selection] = {
                    "kind": "search",
                    "actor": actor,
                    "session": session,
                    "stamp": current_stamp,
                    "version": version,
                    "mode": mode,
                    "candidates": candidates,
                    "expires": now + 600,
                }
                if event is not None:
                    self.chat_search[session] = selection
            else:
                self.chat_search.pop(session, None)
            return {
                "selection": selection if candidates else "",
                "version": version,
                "members": [
                    {"id": row["id"], "name": row["name"]} for row in candidates
                ],
            }
        saved = self.pending.get(selection if operation == "preview" else token)
        if (
            not saved
            or saved["actor"] != actor
            or saved["session"] != session
            or saved["stamp"] != current_stamp
            or saved["version"] != version
            or saved["kind"] != ("search" if operation == "preview" else "preview")
        ):
            raise OpenError("member_whitelist_changed")
        mode = saved["mode"]
        if operation == "preview":
            if type(index) is not int or not 1 <= index <= len(saved["candidates"]):
                raise OpenError("member_whitelist_selection")
            candidate = saved["candidates"][index - 1]
        elif operation == "commit":
            candidate = saved["member"]
            if event is not None and saved["message"] == str(
                event.message_obj.message_id
            ):
                raise OpenError("member_whitelist_confirmation")
        else:
            raise OpenError("member_whitelist_arguments")
        live = eligible.get(candidate["id"], {})
        entry = entries.get(candidate["id"])
        if (
            mode == "add"
            and (
                str(live.get("nimId", "")) != candidate["peer"]
                or member_name(live) != candidate["name"]
                or entry is not None
            )
            or mode == "remove"
            and (entry is None or entry["peer"] != candidate["peer"])
        ):
            raise OpenError("member_whitelist_changed")
        guard()
        if operation == "preview":
            token = secrets.token_urlsafe(18)
            self.pending.pop(selection, None)
            self.pending[token] = {
                **saved,
                "kind": "preview",
                "member": candidate,
                "message": str(event.message_obj.message_id)
                if event is not None
                else "",
            }
            return {
                "token": token,
                "version": version,
                "mode": saved["mode"],
                "member": {"id": candidate["id"], "name": candidate["name"]},
            }
        self.pending.pop(token, None)
        await asyncio.to_thread(
            set_exemption,
            adapter.config["id"],
            account,
            group,
            candidate["id"],
            candidate["peer"],
            enabled=saved["mode"] == "add",
            actor="server/" + actor,
            reason="Confirmed group speech exemption: " + saved["mode"],
            name=candidate["name"],
            guard=guard,
            expected_version=version,
        )
        return {"saved": True, "mode": saved["mode"], "member": candidate["name"]}

    async def command(self, event, command):
        """Resolve chat scope from the existing authorized group selection only.

        Args:
            event: Real native sender and conversation.
            command: Recognized fixed whitelist command.

        Returns:
            Literal workflow feedback, never a model-generated success claim.
        """
        action, _, argument = command.partition(" ")
        session = self.service.navigation.key(event)
        group = self.service.navigation.stamp(event)[3]
        if not group:
            return "请先发送「群列表」「选择群 序号」，再搜索该群成员。"
        options = {}
        operation = "read"
        if action in {"添加白名单", "移除白名单"}:
            if not argument:
                return "请输入「" + action + " 对方名称」，从本群搜索结果中选择。"
            operation = "search"
            options = {
                "mode": "add" if action == "添加白名单" else "remove",
                "query": argument,
            }
        elif action == "选择白名单":
            if not argument.isascii() or not argument.isdecimal():
                return "请先按对方名称搜索，再发送「选择白名单 搜索结果序号」。"
            operation = "preview"
            options = {
                "selection": self.chat_search.get(session, ""),
                "index": int(argument),
            }
        elif action == "确认白名单":
            operation = "commit"
            options = {"token": argument}
        result = await self.run(
            event.platform,
            group,
            operation,
            actor=event.actor_identity.principal,
            session=session,
            event=event,
            **options,
        )
        if operation in {"read", "search"}:
            lines = [
                f"{n}. {row['name']} · UID {row['id']}"
                for n, row in enumerate(result["members"], 1)
            ]
            if operation == "read":
                return (
                    "【本群发言白名单】\n"
                    + ("\n".join(lines) or "暂无本群成员。")
                    + (
                        "\n添加或移除：发送「添加白名单 名称」或「移除白名单 名称」，搜索后选择并确认。"
                        "\n仅豁免本群自动禁言、撤回；不授予管理权限，不会自动解除已有禁言。"
                    )
                )
            return "【本群成员搜索结果】\n" + (
                "\n".join(lines) + "\n发送「选择白名单 序号」，核对后再确认。"
                if lines
                else "没有符合条件的本群成员，请核对名称或白名单状态后重新搜索。"
            )
        if operation == "preview":
            return (
                f"预览：{'加入' if result['mode'] == 'add' else '移出'}本群发言白名单\n"
                f"{result['member']['name']} · UID {result['member']['id']}\n"
                "仅影响本群自动处罚，不授予管理权限，不自动解禁已有禁言。\n"
                f"请在当前对话发新消息：确认白名单 {result['token']}\n十分钟有效。"
            )
        return f"已{'加入' if result['mode'] == 'add' else '移出'}本群发言白名单：{result['member']}。"
