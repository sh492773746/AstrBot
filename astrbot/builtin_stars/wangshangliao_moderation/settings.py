"""Account-bound business policy persistence, migration and hot publication."""

import asyncio
import copy
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.core.platform.sources.wangshangliao.policy import validate_policy
from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
    BUSINESS_FIELDS,
    PolicySnapshot,
    runtime,
)
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def business_revision(config: dict) -> str:
    """Fingerprint business settings, independently of transport settings."""
    if config.get("_business_revision"):
        return str(config["_business_revision"])
    values = {key: config[key] for key in BUSINESS_FIELDS if key in config}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def validate_settings(values: dict) -> None:
    """Validate the complete business policy without granting new capabilities.

    Args:
        values: Whitelisted business settings.

    Raises:
        ValueError: On malformed groups, capabilities, routes or test settings.
    """
    if not isinstance(values, dict) or set(values) - BUSINESS_FIELDS:
        raise ValueError("business_settings_fields")
    groups = values.get("enabled_groups", [])
    if (
        not isinstance(groups, list)
        or len(groups) > 100
        or any(
            not isinstance(g, str)
            or not g.isascii()
            or not g.isdigit()
            or not 0 < int(g) < 1 << 30
            for g in groups
        )
        or len(set(groups)) != len(groups)
    ):
        raise ValueError("enabled_groups_invalid")
    if type(values.get("reply_private", True)) is not bool:
        raise ValueError("reply_private_invalid")
    replies = values.get("reply_groups", {})
    if not isinstance(replies, dict) or any(
        not isinstance(g, str)
        or not g.isascii()
        or not g.isdigit()
        or int(g) <= 0
        or type(v) is not bool
        for g, v in replies.items()
    ):
        raise ValueError("reply_groups_invalid")
    routes = values.get("ai_routes", {})
    if (
        not isinstance(routes, dict)
        or set(routes) - {"admin_provider_id", "groups"}
        or not isinstance(routes.get("admin_provider_id", ""), str)
        or len(routes.get("admin_provider_id", "")) > 256
        or not isinstance(routes.get("groups", {}), dict)
        or len(routes.get("groups", {})) > 100
        or any(
            not isinstance(g, str) or not isinstance(v, str) or len(v) > 256
            for g, v in routes.get("groups", {}).items()
        )
    ):
        raise ValueError("ai_routes_invalid")
    moderation = values.get("moderation", {})
    validate_policy(moderation)
    if moderation.get("enabled") and any(
        enabled
        and g in groups
        and "rename" not in moderation.get("permissions", {}).get(g, [])
        for g, enabled in moderation.get("card_auto", {}).items()
    ):
        raise ValueError("请授权本群修改成员群名片，再开启自动规范")
    proactive = values.get("proactive_send", {})
    if (
        not isinstance(proactive, dict)
        or set(proactive) - {"enabled", "targets"}
        or type(proactive.get("enabled", False)) is not bool
        or not isinstance(proactive.get("targets", []), list)
        or len(proactive.get("targets", [])) > 1000
        or any(
            not isinstance(t, str) or not t or len(t) > 2048
            for t in proactive.get("targets", [])
        )
    ):
        raise ValueError("proactive_send_invalid")
    draft = values.get("developer_test", {})
    if (
        not isinstance(draft, dict)
        or set(draft)
        - {"sender_instance", "groups", "scopes", "keywords", "seconds", "budget"}
        or not isinstance(draft.get("sender_instance", ""), str)
        or len(draft.get("sender_instance", "")) > 256
        or not isinstance(draft.get("groups", []), list)
        or any(not isinstance(g, str) for g in draft.get("groups", []))
        or not isinstance(draft.get("scopes", []), list)
        or any(
            not isinstance(s, str)
            or s
            not in {
                "private_commands",
                "private_ai",
                "group_commands",
                "group_ai",
                "group_rules",
            }
            for s in draft.get("scopes", [])
        )
        or not isinstance(draft.get("keywords", []), list)
        or len(draft.get("keywords", [])) > 10
        or any(
            not isinstance(w, str) or not w.startswith("WSL_TEST_") or len(w) > 100
            for w in draft.get("keywords", [])
        )
        or type(draft.get("seconds", 300)) is not int
        or not 1 <= draft.get("seconds", 300) <= 300
        or type(draft.get("budget", 10)) is not int
        or not 1 <= draft.get("budget", 10) <= 10
    ):
        raise ValueError("developer_test_invalid")


class BusinessSettings:
    """Use one transactional writer for web, commands and AI configuration."""

    def __init__(self, context, root: Path | None = None):
        self.context = context
        self.production_root = root is None
        self.root = (
            root
            or Path(get_astrbot_data_path())
            / "plugin_data"
            / "wangshangliao_moderation"
        )
        self.path = self.root / "settings.sqlite3"
        self.lock = asyncio.Lock()
        self.active = False
        self.initialized = False
        self.keyword_defaults = json.loads(
            Path(__file__)
            .with_name("keyword_defaults.json")
            .read_text(encoding="utf-8")
        )
        if not isinstance(self.keyword_defaults, dict) or set(
            self.keyword_defaults
        ) != {"mute_keywords", "kick_keywords"}:
            raise ValueError("keyword_defaults_fields")
        validate_policy(self.keyword_defaults)
        from .flood import FloodWindows

        self.flood_windows = FloodWindows()

    def observe(self, snapshot, account, group, message, payload, received_at):
        """Freeze deterministic rule evidence without network or disk IO.

        Args:
            snapshot: Immutable account-bound policy at receive time.
            account: Real collector account.
            group: Business group ID.
            message: Upstream message ID.
            payload: Trusted new envelope to enrich.
            received_at: Local ingress timestamp.
        """
        from astrbot.core.platform.sources.wangshangliao.event import is_managed_account

        from .syntax import recognize_command

        config = snapshot.values()
        if "_open_groups" in config:
            from .open_service import recognize, recognize_integrated

            recognize_command = (
                recognize_integrated if config.get("_open_integrated") else recognize
            )
        payload["_dispatch_class"] = (
            "command" if recognize_command(str(payload.get("text", ""))) else "message"
        )
        if group.startswith("private/"):
            from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                runtime,
            )

            finance = getattr(runtime.open_service, "finance_chat", None)
            if finance is not None:
                finance.observe(account, group, payload)
        from astrbot.core.platform.sources.wangshangliao.group_scope import (
            group_config,
            group_revision,
        )

        config["_business_revision"] = snapshot.revision
        revision = group_revision(config, group)
        if "_open_groups" in config:
            payload["_open_revision"] = revision
        policy = group_config(config, group).get("moderation", {})
        sender = str(payload.get("sender", ""))
        protected_sender = is_managed_account(sender)
        test_scope = ""
        if protected_sender:
            from astrbot.core.platform.sources.wangshangliao.runtime_policy import (
                runtime,
            )
            from astrbot.core.platform.sources.wangshangliao.test_rules import (
                admit_rule_target,
            )

            service = runtime.open_service
            receiver = (
                next(
                    (bot for bot in service.adapters() if bot.account == account), None
                )
                if service is not None and getattr(service, "integrated", False)
                else None
            )
            protected_sender = not (
                receiver is not None
                and admit_rule_target(receiver, group, message, payload)
            )
            if not protected_sender:
                window = receiver.test_window
                if not getattr(window, "_rule_generation", ""):
                    window._rule_generation = secrets.token_hex(16)
                test_scope = window._rule_generation
                payload["_test_rule_window"] = test_scope
        if (
            group not in config.get("enabled_groups", [])
            or not policy.get("enabled")
            or not policy.get("automation_enabled")
            or str(payload.get("sender", "")) == account
            or protected_sender
            or str(payload.get("sender", ""))
            in config.get("_open_managers", {}).get(group, [])
        ):
            return
        if (
            policy.get("advertising_enabled") is not False
            and not policy.get("semantic", {}).get("enabled")
            or policy.get("video_whitelist_only") is True
            and payload.get("kind") == "video"
        ) and payload["_dispatch_class"] != "command":
            from .content_rules import content_category
            from .policy import is_fresh_message

            category = content_category(
                payload,
                {
                    **policy,
                    "custom_rules": config.get("_open_groups", {})
                    .get(group, {})
                    .get("custom_rules", []),
                },
            )
            if category and is_fresh_message(payload, received_at):
                payload["_ingress_content"] = {
                    "category": category,
                    "received_at": received_at,
                    "revision": revision,
                    "account": account,
                    "group": group,
                    "sender": sender,
                    "message": str(message),
                }
        if policy.get("flood_enabled") is False:
            return
        payload["_ingress_flood"] = {
            **self.flood_windows.observe(
                account,
                group,
                message,
                payload,
                received_at,
                revision=revision,
                scope=test_scope,
            ),
            "revision": revision,
        }

    def restore_flood(self, instance, account):
        """Read only recent durable ingress evidence before activating business.

        Args:
            instance: Stable native instance ID.
            account: Real collector account.

        Returns:
            Chronological envelope tuples; missing stores return an empty list.
        """
        from astrbot.core.platform.sources.wangshangliao.storage import instance_dir

        path = instance_dir(instance) / "messages.sqlite3"
        if not path.is_file():
            return []
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT team,message,payload,received_at FROM inbox WHERE account=? "
                "AND received_at>=? ORDER BY received_at DESC,rowid DESC LIMIT 10000",
                (account, time.time() - 30),
            ).fetchall()
        return [
            (group, mid, payload, stamp)
            for group, mid, encoded, stamp in reversed(rows)
            if isinstance((payload := json.loads(encoded)), dict)
            and payload.get("_ingress_flood", {}).get("live")
        ]

    @contextmanager
    def connect(self):
        """Close every connection and commit or roll back each transaction."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        if not self.initialized:
            # The sandboxed cashier reads this low-write store without writable WAL sidecars.
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policies(
                    instance TEXT PRIMARY KEY, account TEXT NOT NULL,
                    version INTEGER NOT NULL, payload TEXT NOT NULL,
                    effective_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS policy_history(
                    id INTEGER PRIMARY KEY, instance TEXT NOT NULL,
                    account TEXT NOT NULL, version INTEGER NOT NULL,
                    effective_at REAL NOT NULL, actor TEXT NOT NULL,
                    before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                    UNIQUE(instance,account,version));
                CREATE TABLE IF NOT EXISTS settings_log(
                    id INTEGER PRIMARY KEY, created REAL NOT NULL,
                    instance TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    outcome TEXT NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            self.initialized = True
            self.path.chmod(0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def log(self, instance, actor, action, outcome, code="", detail=None):
        """Record policy operations without including transport credentials."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings_log(created,instance,actor,action,outcome,code,detail) VALUES(?,?,?,?,?,?,?)",
                (
                    time.time(),
                    instance,
                    actor,
                    action,
                    outcome,
                    code,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )

    def load(self, config: dict) -> PolicySnapshot:
        """Load an existing account binding or import legacy settings once."""
        instance, account = str(config["id"]), str(config["account_id"])
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM policies WHERE instance=?", (instance,)
            ).fetchone()
            if row and row["account"] != account:
                raise ValueError("business_account_binding_conflict")
            if row:
                validate_settings(json.loads(row["payload"]))
            if not row:
                if not BUSINESS_FIELDS.intersection(config):
                    raise ValueError("business_policy_missing")
                values = {
                    k: copy.deepcopy(config[k]) for k in BUSINESS_FIELDS if k in config
                }
                values.setdefault("enabled_groups", [])
                values.setdefault("reply_private", True)
                values.setdefault("reply_groups", {})
                values.setdefault("ai_routes", {})
                values.setdefault("proactive_send", {"enabled": False, "targets": []})
                values.setdefault("moderation", {})
                if isinstance(values["moderation"], dict):
                    for field, words in self.keyword_defaults.items():
                        if (
                            field == "mute_keywords"
                            and "keywords" in values["moderation"]
                        ):
                            continue
                        values["moderation"].setdefault(field, copy.deepcopy(words))
                values.setdefault("developer_test", {})
                validate_settings(values)
                payload, now = (
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                    time.time(),
                )
                db.execute(
                    "INSERT INTO policies VALUES(?,?,?,?,?)",
                    (instance, account, 1, payload, now),
                )
                db.execute(
                    "INSERT INTO policy_history(instance,account,version,effective_at,actor,before_json,after_json) VALUES(?,?,?,?,?,?,?)",
                    (instance, account, 1, now, "migration", "{}", payload),
                )
                row = db.execute(
                    "SELECT * FROM policies WHERE instance=?", (instance,)
                ).fetchone()
        snapshot = PolicySnapshot(
            account, f"{account}:{row['version']}", row["payload"], row["effective_at"]
        )
        runtime.publish(instance, snapshot)
        return snapshot

    async def start(self, classifier, *, help_responder=None):
        """Serialize migration against native account registration and updates.

        Args:
            classifier: Plugin-owned command classifier.
            help_responder: Optional plugin-owned builtin help handler.
        """
        from astrbot.core.platform.sources.wangshangliao.registration import (
            registrations,
        )

        async with registrations.commit_lock, self.lock:
            if self.active and runtime.service is self:
                return
            if runtime.service is not None and runtime.service is not self:
                raise ValueError("business_service_already_registered")
            await self._activate(classifier, help_responder=help_responder)

    async def _activate(self, classifier, *, help_responder=None):
        """Import before activation, then remove legacy writable policy fields.

        Args:
            classifier: Plugin-owned command classifier.
            help_responder: Optional plugin-owned builtin help handler.
        """
        config = self.context.get_config()
        platforms = config.get("platform", [])
        if self.production_root and any(
            b.get("type") == "wangshangliao" and BUSINESS_FIELDS.intersection(b)
            for b in platforms
        ):
            from .migration import backup

            destination = (
                self.root
                / "backups"
                / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            )
            backup(Path(get_astrbot_data_path()), destination)
        for bot in platforms:
            if bot.get("type") == "wangshangliao":
                try:
                    self.load(bot)
                except (ValueError, TypeError, KeyError, sqlite3.Error):
                    runtime.snapshots.pop(str(bot.get("id", "")), None)
                    self.log(
                        str(bot.get("id", "")),
                        "system",
                        "migration",
                        "failed",
                        "settings_import_failed",
                    )
                    logger.exception("Wangshangliao policy import failed")
        previous = copy.deepcopy(platforms)
        for bot in platforms:
            if bot.get("type") == "wangshangliao" and runtime.snapshot(bot):
                for key in BUSINESS_FIELDS:
                    bot.pop(key, None)
        try:
            if platforms != previous:
                committed = await config.save_config_async()
                if committed is False:
                    raise ValueError("configuration_persistence_superseded")
        except Exception:
            platforms[:] = previous
            self.log("", "system", "migration", "failed", "transport_cleanup_failed")
            raise
        for bot in platforms:
            if bot.get("type") == "wangshangliao" and runtime.snapshot(bot):
                rows = await asyncio.to_thread(
                    self.restore_flood, str(bot["id"]), str(bot.get("account_id", ""))
                )
                self.flood_windows.restore(str(bot.get("account_id", "")), rows)
        runtime.service, runtime.classifier = self, classifier
        runtime.observer = self.observe
        runtime.help_responder = help_responder
        runtime.generation += 1
        self.active = True
        for adapter in self.context.platform_manager.platform_insts:
            if (
                getattr(adapter, "meta", lambda: None)()
                and adapter.meta().name == "wangshangliao"
            ):
                ledger_path = getattr(getattr(adapter, "ledger", None), "path", None)
                if isinstance(ledger_path, Path):
                    from astrbot.core.platform.sources.wangshangliao.progression_store import (
                        ProgressionStore,
                    )

                    from .review_store import review_store

                    store = review_store(
                        adapter, ledger_path.parent / "moderation.sqlite3"
                    )
                    await asyncio.to_thread(store.initialize)
                    adapter._progression_store = ProgressionStore(
                        ledger_path.parent / "moderation.sqlite3"
                    )
                    await asyncio.to_thread(adapter._progression_store.initialize)
                adapter.test_window = None
                if hasattr(adapter, "dispatch_wakeup"):
                    adapter.dispatch_wakeup.set()
                if (
                    hasattr(adapter, "load_groups")
                    and adapter.connection_state == "online"
                ):
                    try:
                        await adapter.load_groups()
                    except Exception:
                        self.log(
                            adapter.meta().id,
                            "system",
                            "directory_refresh",
                            "failed",
                            "directory_unavailable",
                        )
                        logger.exception(
                            "Wangshangliao directory refresh failed during policy activation"
                        )

    async def save(self, config: dict, expected: str, actor: str) -> str:
        """Commit with compare-and-swap and publish without transport restart.

        Args:
            config: Effective config containing the proposed business settings.
            expected: Current account-bound business revision.
            actor: Authenticated actor, never inferred from a model response.

        Returns:
            New committed revision.

        Raises:
            ValueError: On a stopped service, stale revision or invalid proposal.
        """
        instance, account = str(config["id"]), str(config["account_id"])
        values = {k: copy.deepcopy(config[k]) for k in BUSINESS_FIELDS if k in config}
        try:
            validate_settings(values)
            async with self.lock:
                if not self.active or runtime.service is not self:
                    raise ValueError("business_service_inactive")
                current_bot = next(
                    (
                        b
                        for b in self.context.get_config().get("platform", [])
                        if b.get("id") == instance and b.get("type") == "wangshangliao"
                    ),
                    None,
                )
                if not current_bot or str(current_bot.get("account_id")) != account:
                    raise ValueError("business_account_binding_conflict")
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT * FROM policies WHERE instance=?", (instance,)
                    ).fetchone()
                    if (
                        not row
                        or row["account"] != account
                        or expected != f"{account}:{row['version']}"
                    ):
                        raise ValueError("configuration_changed_preview_again")
                    payload, now, version = (
                        json.dumps(values, ensure_ascii=False, sort_keys=True),
                        time.time(),
                        row["version"] + 1,
                    )
                    db.execute(
                        "UPDATE policies SET version=?,payload=?,effective_at=? WHERE instance=? AND account=?",
                        (version, payload, now, instance, account),
                    )
                    db.execute(
                        "INSERT INTO policy_history(instance,account,version,effective_at,actor,before_json,after_json) VALUES(?,?,?,?,?,?,?)",
                        (
                            instance,
                            account,
                            version,
                            now,
                            actor,
                            row["payload"],
                            payload,
                        ),
                    )
                    db.execute(
                        "INSERT INTO settings_log(created,instance,actor,action,outcome,code,detail) VALUES(?,?,?,?,?,?,?)",
                        (
                            now,
                            instance,
                            actor,
                            "settings_save",
                            "committed",
                            "",
                            json.dumps({"revision": f"{account}:{version}"}),
                        ),
                    )
                snapshot = PolicySnapshot(account, f"{account}:{version}", payload, now)
                previous_groups = set(
                    json.loads(row["payload"]).get("enabled_groups", [])
                )
                runtime.publish(instance, snapshot)
                for adapter in self.context.platform_manager.platform_insts:
                    if adapter.config.get("id") == instance:
                        adapter.test_window = None
                        if hasattr(adapter, "dispatch_wakeup"):
                            adapter.dispatch_wakeup.set()
                        if (
                            previous_groups != set(values.get("enabled_groups", []))
                            and hasattr(adapter, "load_groups")
                            and adapter.connection_state == "online"
                        ):
                            try:
                                await adapter.load_groups()
                            except Exception:
                                self.log(
                                    instance,
                                    actor,
                                    "directory_refresh",
                                    "failed",
                                    "directory_unavailable",
                                )
                                logger.exception(
                                    "Wangshangliao directory refresh failed after policy commit"
                                )
                return snapshot.revision
        except (ValueError, sqlite3.Error) as exc:
            try:
                self.log(
                    instance,
                    actor,
                    "settings_save",
                    "rejected",
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "settings_database_error",
                )
            except sqlite3.Error:
                logger.exception("Wangshangliao policy rejection audit unavailable")
            raise

    async def close(self):
        """Revoke all business execution without logging out the transport."""
        self.active = False
        runtime.detach(self)
        for adapter in self.context.platform_manager.platform_insts:
            if adapter.config.get("type") == "wangshangliao":
                adapter.test_window = None
                if hasattr(adapter, "dispatch_wakeup"):
                    adapter.dispatch_wakeup.set()
                for event in list(getattr(adapter, "events", [])):
                    event.processing_completion.cancel()
                    task = getattr(event, "processing_task", None)
                    if isinstance(task, asyncio.Task):
                        task.cancel()
                db = getattr(getattr(adapter, "ledger", None), "db", None)
                if db is not None:
                    await db.execute(
                        "UPDATE inbox SET state='business_suspended',completed_at=? "
                        "WHERE account=? AND state='pending'",
                        (time.time(), adapter.account),
                    )
                    await db.execute(
                        "UPDATE reply_recalls SET state='business_suspended' "
                        "WHERE account=? AND state='pending'",
                        (adapter.account,),
                    )
                    await db.commit()

    def history(self, instance: str, before: int = 0) -> dict:
        """Return bounded policy changes and errors for the authorized dashboard."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM policy_history WHERE instance=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 50",
                (instance, before, before),
            ).fetchall()
            logs = db.execute(
                "SELECT * FROM settings_log WHERE instance IN (?, '') ORDER BY id DESC LIMIT 100",
                (instance,),
            ).fetchall()
        return {"changes": [dict(r) for r in rows], "logs": [dict(r) for r in logs]}
