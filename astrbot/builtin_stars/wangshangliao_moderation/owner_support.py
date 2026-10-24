"""Dedicated public business support shared by one verified owner across robots."""

import asyncio
import hashlib
import json
import time

from .open_store import OpenError

SELECTION_COMMANDS = {"客服列表", "选择客服", "退出客服"}
TEMPLATE = "template:owner-support"


class OwnerSupport:
    """Keep ownership separate from group, finance and legacy account grants."""

    def __init__(self, service):
        self.service, self.store = service, service.store
        self.choices, self.selections = {}, {}
        self.provision_lock = asyncio.Lock()
        with self.store.connect(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS support_owners(
                    uid TEXT PRIMARY KEY,peer TEXT NOT NULL,name TEXT NOT NULL,
                    version INTEGER NOT NULL,active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS support_group_owners(
                    account TEXT,group_id TEXT,epoch INTEGER,uid TEXT NOT NULL,
                    PRIMARY KEY(account,group_id));
                CREATE TABLE IF NOT EXISTS support_owner_drafts(
                    token TEXT PRIMARY KEY,account TEXT NOT NULL,context TEXT NOT NULL);
            """)

    def rows(self):
        """Return server-owned identity metadata, never prompts or credentials."""
        with self.store.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM support_owners ORDER BY uid")
            ]

    def available(self, account, *, editing=False):
        """Resolve live epoch-bound businesses on exactly the receiving robot."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT o.*,t.group_id,t.managers,t.rules_only,t.version AS tenant_version "
                "FROM support_owners o JOIN support_group_owners b ON b.uid=o.uid "
                "JOIN tenants t ON t.account=b.account AND t.group_id=b.group_id "
                "WHERE b.account=? AND b.epoch=t.epoch AND o.active=1 "
                "AND t.active=1 AND t.present=1 AND t.paused=0 "
                "AND t.cleanup_epoch<=t.cleanup_done "
                "AND t.admission IN ('legacy','verified') ORDER BY o.uid,t.group_id",
                (account,),
            ).fetchall()
        owners = {}
        for row in rows:
            if row["uid"] not in json.loads(row["managers"]):
                continue
            item = owners.setdefault(
                row["uid"],
                {
                    "uid": row["uid"],
                    "peer": row["peer"],
                    "name": row["name"],
                    "version": row["version"],
                    "scope": "owner:" + row["uid"],
                    "rules_only": True,
                    "bindings": [],
                },
            )
            item["rules_only"] &= bool(row["rules_only"])
            item["bindings"].append((row["group_id"], row["tenant_version"]))
        # Public callers receive only a fingerprint of bindings, not other groups.
        for item in owners.values():
            item["binding"] = hashlib.sha256(
                json.dumps(item.pop("bindings")).encode()
            ).hexdigest()
            if not editing:
                item.pop("peer")
        result = list(owners.values())
        if not editing:
            # A new dedicated business must not silently replace real legacy content.
            with self.store.connect() as db:
                bound = db.execute(
                    "SELECT 1 FROM support_group_owners WHERE account=? LIMIT 1",
                    (account,),
                ).fetchone()
                legacy_knowledge = db.execute(
                    "SELECT 1 FROM knowledge_entries WHERE kind='support' AND owner='' "
                    "AND account=? AND group_id='' LIMIT 1",
                    (account,),
                ).fetchone()
            if bound and (
                self.service.support.read(account)["version"] or legacy_knowledge
            ):
                result.append(
                    {
                        "uid": "legacy",
                        "name": "账号" + account + "原有业务客服",
                        "version": self.service.support.read(account)["version"],
                        "scope": account,
                        "rules_only": False,
                        "binding": self.service.support.fingerprint(account),
                    }
                )
        return result

    def editor(self, identity):
        """Require independently verified UID/peer on the receiving account."""
        if (
            identity.verified_uid != identity.claimed_uid
            or not identity.peer
            or not self.store.verified_actor(
                identity.account, identity.claimed_uid, identity.peer
            )
        ):
            return None
        owner = next(
            (
                row
                for row in self.available(identity.account, editing=True)
                if (row["uid"], row["peer"]) == (identity.verified_uid, identity.peer)
            ),
            None,
        )
        return {**owner, "account": identity.account} if owner else None

    def route(self, event):
        """Snapshot a public selection without inferring business from nicknames."""
        account = str(event.platform.account)
        key = (account, event.actor_identity.principal, str(event.unified_msg_origin))
        options = self.available(account)
        with self.store.connect() as db:
            bound = bool(
                db.execute(
                    "SELECT 1 FROM support_group_owners WHERE account=? LIMIT 1",
                    (account,),
                ).fetchone()
            )
        selected = self.selections.get(key)
        explicit = bool(selected and selected["expires"] > time.monotonic())
        target = next(
            (row for row in options if explicit and row["uid"] == selected["uid"]),
            None,
        )
        invalid = explicit and target is None
        if (
            target is None
            and len(options) == 1
            and options[0]["uid"] != "legacy"
            and not invalid
        ):
            target = options[0]
        return {
            "target": target,
            "ambiguous": bool(options) and target is None,
            "unavailable": invalid or bound and not options,
            "explicit": explicit and target is not None,
            "options": options,
        }

    async def command(self, event, command):
        """Select only a currently published business through fixed private commands."""
        if not event.is_private_chat():
            raise OpenError("support_private_only")
        guard = self.service.write_guard(event.platform, event)
        guard()
        key = (
            str(event.platform.account),
            event.actor_identity.principal,
            str(event.unified_msg_origin),
        )
        now = time.monotonic()
        self.choices = {k: v for k, v in self.choices.items() if v["expires"] > now}
        self.selections = {
            k: v for k, v in self.selections.items() if v["expires"] > now
        }
        action, _, argument = command.partition(" ")
        if action == "退出客服":
            self.selections.pop(key, None)
            self.choices.pop(key, None)
            return "已退出指定客服；后续按当前身份重新分流。"
        options = self.available(key[0])
        if action == "客服列表":
            if not options:
                return "本机器人暂无已绑定的专属客服；原账号客服保持不变。"
            if len(self.choices) >= 1024 and key not in self.choices:
                raise OpenError("support_busy")
            self.choices[key] = {"options": options, "expires": now + 900}
            return (
                "【专属客服】\n"
                + "\n".join(
                    f"{n}. {row['name']}"
                    + (f"（UID {row['uid']}）" if row["uid"] != "legacy" else "")
                    + (" · 仅规则功能，AI未开通" if row["rules_only"] else "")
                    for n, row in enumerate(options, 1)
                )
                + "\n发送：选择客服 序号；退出客服 可恢复身份分流。"
            )
        if action != "选择客服":
            raise OpenError("support_selection_invalid")
        previous = self.choices.get(key)
        if not previous or previous["options"] != options:
            raise OpenError("support_selection_changed")
        if not argument.isascii() or not argument.isdigit():
            raise OpenError("support_selection_invalid")
        index = int(argument) - 1
        if not 0 <= index < len(options):
            raise OpenError("support_selection_invalid")
        if len(self.selections) >= 1024 and key not in self.selections:
            raise OpenError("support_busy")
        guard()
        target = options[index]
        self.selections[key] = {"uid": target["uid"], "expires": now + 900}
        return f"已选择 {target['name']} 的专属客服。" + (
            "当前仅规则功能，AI咨询未开通。"
            if target["rules_only"]
            else "可发送 /chat 咨询。"
        )

    def state(self, uid, version, active, actor, *, guard):
        """Revoke or restore one business with CAS, audit and no AI entitlement."""
        if type(version) is not int or type(active) is not bool:
            raise OpenError("support_owner_invalid")
        with self.store.connect(write=True, guard=guard) as db:
            row = db.execute(
                "SELECT * FROM support_owners WHERE uid=?", (uid,)
            ).fetchone()
            if not row or row["version"] != version:
                raise OpenError("support_owner_changed")
            db.execute(
                "UPDATE support_owners SET active=?,version=version+1 WHERE uid=?",
                (int(active), uid),
            )
            self.store.log(
                db,
                actor,
                "",
                "",
                "support_owner_state",
                {"uid": uid, "active": active, "version": version + 1},
            )
        return {"saved": True, "version": version + 1}

    async def ensure_profile(self, uid, actor, *, guard):
        """Retry one deterministic profile creation without copying legacy business data."""
        async with self.provision_lock:
            owner = next((row for row in self.rows() if row["uid"] == uid), None)
            if not owner or not owner["active"]:
                raise OpenError("support_owner_unavailable")
            frozen = dict(owner)

            def current():
                guard()
                if next((r for r in self.rows() if r["uid"] == uid), None) != frozen:
                    raise OpenError("support_owner_changed")

            return await self.service.support.provision(
                "owner:" + uid, actor, guard=current, owner_name=owner["name"]
            )
