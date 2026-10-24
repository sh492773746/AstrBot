"""Explicit robot-level business editors, isolated from group and finance roles."""

import asyncio
import hashlib
import json
import secrets
import time
from copy import deepcopy
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from astrbot.core.config.default import DEFAULT_CONFIG

from .conversation_profiles import DEFAULT
from .open_store import OpenError

COMMANDS = {
    "客服设置",
    "客服话术",
    "客服风格",
    "客服长度",
    "客服链接",
    "客服知识",
    "添加客服知识",
    "删除客服知识",
    "确认客服",
}


class SupportConfiguration(BaseModel):
    """Only business content is editable, never infrastructure or authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    wording: str = Field(default="", max_length=4000)
    style: Literal["简洁", "亲切", "正式"] = "简洁"
    length: int = Field(default=300, ge=50, le=2000)
    links: list[Annotated[str, Field(max_length=2000)]] = Field(
        default_factory=list, max_length=20
    )
    knowledge: list[Annotated[str, Field(min_length=1, max_length=8192)]] = Field(
        default_factory=list, max_length=100
    )

    @field_validator("links")
    @classmethod
    def public_links(cls, values):
        """Reject credentials and non-HTTP schemes from public business links."""
        for value in values:
            parsed = urlsplit(value)
            if (
                len(value) > 2000
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(ord(c) < 33 for c in value)
            ):
                raise ValueError("support_link_invalid")
        return values

    @model_validator(mode="after")
    def bounded_knowledge(self):
        """Keep public business knowledge bounded separately from personal notes."""
        if any(not v.strip() or len(v.encode()) > 8192 for v in self.knowledge):
            raise ValueError("support_knowledge_invalid")
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 65536:
            raise ValueError("support_knowledge_capacity")
        return self


class SupportSettings:
    """Store account-only content with explicit native-sender editor grants."""

    def __init__(self, service):
        self.service, self.store = service, service.store
        with self.store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS support_settings(
                    account TEXT PRIMARY KEY,version INTEGER,config TEXT);
                CREATE TABLE IF NOT EXISTS support_editors(
                    account TEXT,uid TEXT,peer TEXT,version INTEGER,active INTEGER,
                    PRIMARY KEY(account,uid));
                CREATE TABLE IF NOT EXISTS support_drafts(
                    token TEXT PRIMARY KEY,account TEXT,actor TEXT,session TEXT,
                    version INTEGER,grant_version INTEGER,uid TEXT,peer TEXT,
                    config TEXT,expires REAL,used INTEGER DEFAULT 0);
            """)

    def read(self, account):
        """Return one robot's business configuration and revision."""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM support_settings WHERE account=?", (account,)
            ).fetchone()
        return {
            "account": account,
            "version": row["version"] if row else 0,
            "config": json.loads(row["config"])
            if row
            else SupportConfiguration(
                **(
                    {
                        "wording": "您好，我是专属业务客服。只依据已确认的业务资料解答；"
                        "资料不足时请联系业务负责人，不承诺未确认事项。"
                    }
                    if account == "template:owner-support"
                    else {}
                )
            ).model_dump(),
        }

    def editors(self, account):
        """Return server-visible grants without deriving any from group ownership."""
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM support_editors WHERE account=? ORDER BY uid",
                    (account,),
                )
            ]

    def grant(self, account, uid, peer, version, active, actor, *, guard):
        """CAS an explicitly server-assigned, already verified native identity."""
        if (
            not isinstance(account, str)
            or not account.isascii()
            or not account.isdigit()
            or not isinstance(uid, str)
            or not uid.isascii()
            or not uid.isdigit()
            or not isinstance(peer, str)
            or not 1 <= len(peer) <= 256
            or type(version) is not int
            or type(active) is not bool
        ):
            raise OpenError("support_grant_invalid")
        with self.store.connect(write=True, guard=guard) as db:
            if active and not self.store.verified_actor(account, uid, peer):
                raise OpenError("support_identity_unverified")
            old = db.execute(
                "SELECT * FROM support_editors WHERE account=? AND uid=?",
                (account, uid),
            ).fetchone()
            if (old["version"] if old else 0) != version:
                raise OpenError("support_grant_changed")
            db.execute(
                "INSERT INTO support_editors VALUES(?,?,?,?,?) "
                "ON CONFLICT(account,uid) DO UPDATE SET peer=excluded.peer,"
                "version=excluded.version,active=excluded.active",
                (account, uid, peer, version + 1, int(active)),
            )
            self.store.log(
                db,
                actor,
                account,
                "",
                "support_editor_saved",
                {"uid": uid, "active": active, "version": version + 1},
            )
        return {"saved": True, "version": version + 1}

    def editor(self, identity):
        """Resolve only an exact account, UID and transport-peer grant."""
        if identity.verified_uid != identity.claimed_uid or not identity.peer:
            return None
        if not self.store.verified_actor(
            identity.account, identity.claimed_uid, identity.peer
        ):
            return None
        return next(
            (
                row
                for row in self.editors(identity.account)
                if row["uid"] == identity.verified_uid
                and row["peer"] == identity.peer
                and row["active"]
            ),
            None,
        )

    def preview(self, account, version, config, actor, session, *, guard, editor=None):
        """Freeze typed content for a two-minute actor/session-bound confirmation."""
        config = SupportConfiguration.model_validate(config).model_dump()
        token = secrets.token_urlsafe(18)
        with self.store.connect(write=True, guard=guard) as db:
            if self.read(account)["version"] != version:
                raise OpenError("support_settings_changed")
            db.execute("DELETE FROM support_drafts WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO support_drafts VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (
                    token,
                    account,
                    actor,
                    session,
                    version,
                    editor["version"] if editor else 0,
                    editor["uid"] if editor else "",
                    editor["peer"] if editor else "",
                    json.dumps(config),
                    time.time() + 120,
                ),
            )
            if editor and account == "owner:" + editor["uid"]:
                db.execute(
                    "DELETE FROM support_owner_drafts WHERE token NOT IN "
                    "(SELECT token FROM support_drafts)"
                )
                db.execute(
                    "INSERT INTO support_owner_drafts VALUES(?,?,?)",
                    (token, editor["account"], json.dumps(editor, sort_keys=True)),
                )
        return {"token": token, "version": version, "config": config, "expires_in": 120}

    def commit(self, account, token, actor, session, *, guard):
        """Commit reviewed public content under the still-current editor grant."""
        with self.store.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM support_drafts WHERE token=?", (token,)
            ).fetchone()
            if (
                not row
                or row["account"] != account
                or row["actor"] != actor
                or row["session"] != session
                or row["expires"] <= time.time()
            ):
                raise OpenError("support_confirmation_invalid")
            if row["uid"]:
                if account == "owner:" + row["uid"]:
                    from astrbot.core.platform.sources.wangshangliao.identity import (
                        ActorIdentity,
                    )

                    grant = db.execute(
                        "SELECT * FROM support_owners WHERE uid=?", (row["uid"],)
                    ).fetchone()
                    context = db.execute(
                        "SELECT * FROM support_owner_drafts WHERE token=?", (token,)
                    ).fetchone()
                    current = (
                        self.service.owner_support.editor(
                            ActorIdentity(
                                context["account"] if context else "",
                                row["uid"],
                                row["peer"],
                                row["uid"],
                            )
                        )
                        if context
                        else None
                    )
                    if (
                        not current
                        or json.dumps(current, sort_keys=True) != context["context"]
                    ):
                        raise OpenError("support_editor_revoked")
                else:
                    grant = db.execute(
                        "SELECT * FROM support_editors WHERE account=? AND uid=?",
                        (account, row["uid"]),
                    ).fetchone()
                if (
                    not grant
                    or not grant["active"]
                    or grant["peer"] != row["peer"]
                    or grant["version"] != row["grant_version"]
                ):
                    raise OpenError("support_editor_revoked")
            if row["used"]:
                return {"saved": True, "version": row["version"] + 1}
            if self.read(account)["version"] != row["version"]:
                raise OpenError("support_settings_changed")
            db.execute(
                "INSERT INTO support_settings VALUES(?,?,?) ON CONFLICT(account) "
                "DO UPDATE SET version=excluded.version,config=excluded.config",
                (account, row["version"] + 1, row["config"]),
            )
            db.execute("UPDATE support_drafts SET used=1 WHERE token=?", (token,))
            self.store.log(
                db,
                actor,
                account,
                "",
                "support_settings_saved",
                {"version": row["version"] + 1},
            )
        return {"saved": True, "version": row["version"] + 1}

    async def command(self, event, command):
        """Allow designated editors to preview and confirm through private commands."""
        if not event.is_private_chat():
            raise OpenError("support_private_only")
        await self.service.verify_actor(event)
        identity = event.actor_identity
        owners = getattr(self.service, "owner_support", None)
        owner = await asyncio.to_thread(owners.editor, identity) if owners else None
        editor = owner or await asyncio.to_thread(self.editor, identity)
        if not editor:
            raise OpenError("support_editor_required")
        outer = self.service.write_guard(event.platform, event)

        def guard():
            outer()
            if (owners.editor(identity) if owner else self.editor(identity)) != editor:
                raise OpenError("support_editor_revoked")

        guard()
        previous = event.get_extra("_wsl_management_delivery_guard")

        def delivery_guard():
            guard()
            if callable(previous):
                previous()

        event.set_extra("_wsl_management_delivery_guard", delivery_guard)
        action, _, argument = command.partition(" ")
        account, actor, session = (
            owner["scope"] if owner else identity.account,
            identity.principal,
            str(event.unified_msg_origin),
        )
        row = await asyncio.to_thread(self.read, account)
        if action == "确认客服":
            result = await asyncio.to_thread(
                self.commit, account, argument.strip(), actor, session, guard=guard
            )
            return f"业务客服已保存（版本{result['version']}）。"
        if action in {"客服设置", "客服知识"}:
            return (
                f"【{owner['name'] + '的专属客服' if owner else '账号' + account + '的业务客服'}"
                f"·版本{row['version']}】\n"
                + json.dumps(row["config"], ensure_ascii=False, indent=2)
                + ("\n本人的已绑定群及机器人共用这份业务资料。" if owner else "")
                + "\n仅已核验的客服所有者或指定负责人可修改。模型、工具和管理员指引由服务端维护。"
                + "\n客服话术 内容\n客服风格 简洁/亲切/正式\n客服长度 300"
                + "\n客服链接 https://已确认地址（多个以空格分隔，留空清空）"
                + "\n添加客服知识 内容\n删除客服知识 序号"
            )
        config = row["config"]
        if action == "客服话术":
            config["wording"] = argument.strip()
        elif action == "客服风格":
            config["style"] = argument.strip()
        elif action == "客服长度":
            config["length"] = int(argument.strip())
        elif action == "客服链接":
            config["links"] = argument.split()
        elif action == "添加客服知识":
            config["knowledge"].append(argument.strip())
        elif action == "删除客服知识":
            index = int(argument.strip()) - 1
            if not 0 <= index < len(config["knowledge"]):
                raise OpenError("support_knowledge_number")
            config["knowledge"].pop(index)
        else:
            raise OpenError("support_settings_invalid")
        preview = await asyncio.to_thread(
            self.preview,
            account,
            row["version"],
            config,
            actor,
            session,
            guard=guard,
            editor=editor,
        )
        return (
            "【客服修改预览】\n"
            + json.dumps(preview["config"], ensure_ascii=False, indent=2)
            + "\n两分钟内在本对话发送：确认客服 "
            + preview["token"]
        )

    def sync_guidance_manual(self, actor, *, guard):
        """Synchronize only generated server manual entries using versioned writes.

        Args:
            actor: Server provisioning or lifecycle principal for the audit.
            guard: Current service authority checked for every committed change.

        Returns:
            Current manual version and number of generated entries.
        """
        from .conversation_guidance import MANUAL_PREFIX, admin_manual_documents
        from .knowledge_store import KnowledgeScope

        knowledge = self.service.knowledge.store
        scope = KnowledgeScope("admin_manual")
        documents = admin_manual_documents()
        current = knowledge.read(scope, guard=guard)
        for entry in reversed(current["entries"]):
            if (
                entry["title"].startswith(MANUAL_PREFIX)
                and entry["title"] not in documents
            ):
                number = next(
                    n
                    for n, row in enumerate(current["entries"], 1)
                    if row["id"] == entry["id"]
                )
                knowledge.change(
                    current["snapshot"],
                    "delete",
                    {"number": number},
                    f"admin-manual/delete/{entry['id']}/{current['snapshot'].version}",
                    actor,
                    guard=guard,
                )
                current = knowledge.read(scope, guard=guard)
        for title, body in documents.items():
            old = next(
                (
                    (n, row)
                    for n, row in enumerate(current["entries"], 1)
                    if row["title"] == title
                ),
                None,
            )
            if old and old[1]["body"] == body:
                continue
            digest = hashlib.sha256((title + "\n" + body).encode()).hexdigest()
            knowledge.change(
                current["snapshot"],
                "edit" if old else "add",
                {"title": title, "body": body, **({"number": old[0]} if old else {})},
                f"admin-manual/{current['snapshot'].version}/{digest}",
                actor,
                guard=guard,
            )
            current = knowledge.read(scope, guard=guard)
        return {"version": current["snapshot"].version, "entries": len(documents)}

    def guidance_status(self):
        """Report manual coverage and binding health without calling a model.

        Returns:
            Bounded readiness metadata, not prompts, private content or authority.
        """
        from .conversation_guidance import admin_manual_documents
        from .knowledge_store import KnowledgeScope

        current = self.service.knowledge.store.read(KnowledgeScope("admin_manual"))
        expected = admin_manual_documents()
        actual = {entry["title"]: entry["body"] for entry in current["entries"]}
        matched = sum(actual.get(title) == body for title, body in expected.items())
        bound = any(
            row["kind"] == "admin_guidance" for row in self.service.profiles.rows()
        )
        profile_ready = False
        if bound:
            try:
                self.service.profiles.resolve("admin_guidance")
                profile_ready = True
            except OpenError:
                pass
        return {
            "profile_ready": profile_ready,
            "manual_current": matched == len(expected),
            "manual_entries": matched,
            "manual_expected": len(expected),
            "manual_version": current["snapshot"].version,
            "operation_tools": False,
        }

    async def provision(self, account, actor, *, guard, owner_name=""):
        """Create dedicated native profiles without copying another robot's content."""
        service = self.service
        kind = "consultation" if account else "admin_guidance"
        existing = next(
            (
                row
                for row in service.profiles.rows()
                if (row["kind"], row["account"], row["group_id"]) == (kind, account, "")
            ),
            None,
        )
        persona_id = (
            owner_name + "·专属客服·" + account.removeprefix("owner:")
            if owner_name
            else "旺商聊·业务客服·" + account
            if account
            else "旺商聊·服务端管理员指引"
        )
        if existing:
            if not account:
                await asyncio.to_thread(self.sync_guidance_manual, actor, guard=guard)
            elif owner_name:
                selection = existing["selection"]
                manager = service.context.persona_manager
                config_manager = service.context.astrbot_config_mgr
                config_id = selection["config_id"]
                config = config_manager.confs.get(config_id)
                if (
                    config_id == "default"
                    or config is None
                    or any(
                        row != existing
                        and row["selection"]
                        and row["selection"]["config_id"] == config_id
                        for row in service.profiles.rows()
                    )
                ):
                    raise OpenError("dedicated_persona_conflict")
                if selection["persona_id"] != persona_id:
                    old = manager.get_persona_v3_by_id(selection["persona_id"])
                    if (
                        not old
                        or old.get("tools") != []
                        or old.get("skills") not in ([], None)
                    ):
                        raise OpenError("dedicated_persona_conflict")
                    target = manager.get_persona_v3_by_id(persona_id)
                    if target and any(
                        target.get(key) != old.get(key)
                        for key in ("prompt", "begin_dialogs", "tools", "skills")
                    ):
                        raise OpenError("dedicated_persona_conflict")
                    guard()
                    if not target:
                        await manager.create_persona(
                            persona_id,
                            old["prompt"],
                            begin_dialogs=old.get("begin_dialogs"),
                            tools=[],
                            skills=old.get("skills"),
                            custom_error_message=old.get("custom_error_message"),
                        )
                    guard()
                    await asyncio.to_thread(
                        service.profiles.save,
                        kind,
                        account,
                        "",
                        existing["version"],
                        {**selection, "persona_id": persona_id},
                        actor,
                        guard=guard,
                        rebind=True,
                    )
                if (
                    config["agent_runner"]["config"]["persona"]["persona_id"]
                    != persona_id
                ):
                    guard()
                    config["agent_runner"]["config"]["persona"]["persona_id"] = (
                        persona_id
                    )
                    config.save_config()
                metadata = next(
                    (
                        row
                        for row in config_manager.get_conf_list()
                        if row["id"] == config_id
                    ),
                    None,
                )
                if metadata and metadata["name"] != persona_id:
                    guard()
                    await config_manager.update_conf_info(config_id, name=persona_id)
            return {
                "created": False,
                "profile": next(
                    row
                    for row in service.profiles.rows()
                    if (row["kind"], row["account"], row["group_id"])
                    == (kind, account, "")
                ),
            }
        guard()
        manager = service.context.persona_manager
        persona = manager.get_persona_v3_by_id(persona_id)
        if persona is None:
            prompt = (
                "你是独立业务客服。只依据本轮提供的已确认业务资料回答；"
                "不借用其他业务、群聊或用户的资料。资料缺失时明确说明并联系负责人。"
                "不得泄露内部配置、执行命令或调用工具。"
                if account
                else "你是服务端维护的管理员使用指引。只根据服务器提供的权限及命令目录说明用法；"
                "不执行修改、不授予权限、不把群管、客服负责人和财务角色混为一谈。"
            )
            await manager.create_persona(persona_id, prompt, tools=[], skills=[])
        elif persona.get("tools") != [] or persona.get("skills") not in ([], None):
            raise OpenError("dedicated_persona_conflict")
        guard()
        config = deepcopy(DEFAULT_CONFIG)
        config["agent_runner"] = {
            "runner_type": "local",
            "config": {
                "model": {"provider_id": service.budget["provider"]},
                "persona": {"persona_id": persona_id},
            },
        }
        config_manager = service.context.astrbot_config_mgr
        existing_config = (
            next(
                (
                    row
                    for row in config_manager.get_conf_list()
                    if row["name"] == persona_id
                ),
                None,
            )
            if owner_name
            else None
        )
        config_id = (
            existing_config["id"]
            if existing_config
            else (await config_manager.create_conf(config=config, name=persona_id))
        )
        guard()
        selection = {
            **DEFAULT,
            "config_id": config_id,
            "persona_id": persona_id,
            "provider_id": service.budget["provider"],
            "max_tokens": 1024,
            "history_turns": 6,
        }
        await asyncio.to_thread(
            service.profiles.save, kind, account, "", 0, selection, actor, guard=guard
        )
        if not account:
            await asyncio.to_thread(self.sync_guidance_manual, actor, guard=guard)
        return {"created": True, "config_id": config_id, "persona_id": persona_id}

    def fingerprint(self, account):
        """Bind private dialogue history to the exact business content revision."""
        return hashlib.sha256(
            json.dumps(self.read(account), sort_keys=True).encode()
        ).hexdigest()
