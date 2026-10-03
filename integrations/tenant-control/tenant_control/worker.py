"""Privileged local deployment worker, separate from the payment bot."""

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from .runtime import TEMPLATE
from .store import Store


class Worker:
    """Create tenant containers from fresh config and reviewed plugin source."""

    def __init__(
        self,
        store: Store,
        cipher: Fernet,
        tenant_root: Path,
        source_root: Path,
        image: str,
        provider_template: Path,
    ):
        if not re.fullmatch(r"(?:[^ ]+@)?sha256:[a-f0-9]{64}", image):
            raise ValueError("A pinned image digest is required")
        self.store = store
        self.cipher = cipher
        self.root = tenant_root.resolve()
        self.source = source_root.resolve()
        self.image = image
        template = json.loads(provider_template.read_text(encoding="utf-8-sig"))
        template.setdefault("embedding_provider_id", "")
        if not (
            isinstance(template.get("provider"), list)
            and template["provider"]
            and isinstance(template.get("chat_provider_id"), str)
            and isinstance(template.get("embedding_provider_id"), str)
            and template["chat_provider_id"]
        ):
            raise ValueError("A reviewed provider template is required")
        names = {p.get("id") for p in template["provider"] if isinstance(p, dict)}
        if (
            not {
                p
                for p in (
                    template["chat_provider_id"],
                    template["embedding_provider_id"],
                )
                if p
            }
            <= names
        ):
            raise ValueError("Provider IDs must exist in the reviewed template")
        if "REPLACE" in json.dumps(template) or ".invalid" in json.dumps(template):
            raise ValueError("Placeholder provider credentials are forbidden")
        chat = next(
            (
                p
                for p in template["provider"]
                if p.get("id") == template["chat_provider_id"]
            ),
            None,
        )
        embedding = next(
            (
                p
                for p in template["provider"]
                if p.get("id") == template["embedding_provider_id"]
            ),
            None,
        )
        if (
            not chat
            or (template["embedding_provider_id"] and not embedding)
            or chat is embedding
            or chat.get("type") != "openai_chat_completion"
            or (embedding and embedding.get("type") != "openai_embedding")
            or chat.get("key") != []
            or (embedding and embedding.get("embedding_api_key") != "")
            or any(
                value
                for provider in template["provider"]
                for name, value in provider.items()
                if "key" in name.lower()
            )
        ):
            raise ValueError("Do not copy platform model keys to tenant data")
        self.proxy_url = os.environ.get("TENANT_PROVIDER_PROXY_URL", "").rstrip("/")
        match = re.fullmatch(
            r"http://host\.docker\.internal:(\d{2,5})/v1", self.proxy_url
        )
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError("Use only the private host Docker bridge model gateway")
        self.proxy_health_url = os.environ.get(
            "TENANT_PROVIDER_HEALTH_URL", "http://127.0.0.1:18734/health"
        )
        self.providers = template
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    @staticmethod
    def _docker(*args: str) -> str:
        result = subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True, timeout=90
        )
        return result.stdout.strip()

    def proxy_ready(self) -> bool:
        """Keep Stars checkout closed when the platform model gateway is down."""
        try:
            with urllib.request.urlopen(self.proxy_health_url, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False

    @staticmethod
    def _container(bot_id: int) -> str:
        return f"astrbot-tenant-{bot_id}"

    def _state(self, bot_id: int) -> str:
        """Refuse to control a container lacking our ownership label."""
        name = self._container(bot_id)
        result = subprocess.run(
            ["docker", "inspect", name, "--format", "{{json .}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            return "missing"
        item = json.loads(result.stdout)
        if item["Config"]["Labels"].get("tenant-control.bot-id") != str(bot_id):
            raise ValueError("Container name belongs to a different service")
        pinned = self._docker("image", "inspect", self.image, "--format", "{{.Id}}")
        if item["Image"] != pinned:
            raise ValueError("Container image differs from the pinned tenant image")
        return "running" if item["State"]["Running"] else "stopped"

    def _healthy(self, bot_id: int) -> bool:
        """Read the tenant's transport heartbeat without an HTTP server."""
        try:
            report = json.loads(
                self._docker(
                    "exec",
                    self._container(bot_id),
                    "cat",
                    "/AstrBot/data/tenant-health.json",
                )
            )
            return (
                report["platform_id"] == f"tenant-{bot_id}"
                and report["ready"] is True
                and 0 <= time.time() - report["at"] < 90
            )
        except (ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return False

    def _prepare(self, row) -> tuple[Path, bool]:
        """Write only new tenant data; never read the production data volume."""
        if str(self.source) not in sys.path:
            sys.path.insert(0, str(self.source))
        from astrbot.core.config.default import DEFAULT_CONFIG

        bot_id = row["id"]
        entitlement = self.store.db.execute(
            "SELECT * FROM tenant_entitlements WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if entitlement and entitlement["template"] != TEMPLATE:
            raise ValueError("Worker does not support this tenant template")
        community_enabled = not entitlement or "community" in json.loads(
            entitlement["features"]
        )
        tenant = self.root / str(bot_id)
        data = tenant / "data"
        config_dir = data / "config"
        plugin_dir = data / "plugins" / "astrbot_plugin_superbot"
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tenant.chmod(0o700)
        data.chmod(0o700)
        plugin_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = self.source / "data" / "plugins" / "astrbot_plugin_superbot"
        if not source.is_dir() or any(item.is_symlink() for item in source.rglob("*")):
            raise ValueError("Untrusted plugin source")
        if not plugin_dir.exists():
            shutil.copytree(
                source,
                plugin_dir,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
        health_source = (
            Path(__file__).resolve().parents[1] / "astrbot_plugin_tenant_health"
        )
        health_target = data / "plugins/astrbot_plugin_tenant_health"
        shutil.copytree(
            health_source,
            health_target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (config_dir / "astrbot_plugin_tenant_health_config.json").write_text(
            json.dumps({"platform_id": f"tenant-{bot_id}"})
        )
        config_path = data / "cmd_config.json"
        plugin_path = config_dir / "astrbot_plugin_superbot_config.json"
        token = self.cipher.decrypt(row["token_cipher"]).decode()
        if row["provider_secret"]:
            model_key = self.cipher.decrypt(row["provider_secret"]).decode()
        else:
            model_key = secrets.token_urlsafe(32)
            with self.store.tx() as db:
                db.execute(
                    "UPDATE bots SET provider_secret=?,provider_key_hash=? "
                    "WHERE id=? AND provider_secret IS NULL",
                    (
                        self.cipher.encrypt(model_key.encode()),
                        hashlib.sha256(model_key.encode()).hexdigest(),
                        bot_id,
                    ),
                )
        token_changed = False
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            platform = config.get("platform") or []
            if len(platform) != 1 or platform[0].get("id") != f"tenant-{bot_id}":
                raise ValueError("Tenant config was modified unexpectedly")
            token_changed = platform[0]["telegram_token"] != token
            platform[0]["telegram_token"] = token
        else:
            config = copy.deepcopy(DEFAULT_CONFIG)
            config["admins_id"] = [str(row["owner_id"])]
            config["platform"] = [
                {
                    "id": f"tenant-{bot_id}",
                    "type": "telegram",
                    "enable": True,
                    "telegram_token": token,
                    "start_message": "欢迎使用社区机器人。",
                }
            ]
            config["provider"] = copy.deepcopy(self.providers["provider"])
            config["plugin_set"] = [
                "astrbot_plugin_superbot",
                "astrbot_plugin_tenant_health",
            ]
        dashboard = config.setdefault("dashboard", {})
        token_changed = token_changed or dashboard.get("enable", True)
        if "astrbot_plugin_tenant_health" not in config.get("plugin_set", []):
            config.setdefault("plugin_set", []).append("astrbot_plugin_tenant_health")
            token_changed = True
        dashboard.update(enable=False, password="", pbkdf2_password="", jwt_secret="")
        self.store.db.execute(
            "UPDATE bots SET dashboard_secret=NULL,dashboard_port=NULL WHERE id=?",
            (bot_id,),
        )
        provider_ids = {provider.get("id") for provider in config.get("provider", [])}
        if (
            not {
                p
                for p in (
                    self.providers["chat_provider_id"],
                    self.providers["embedding_provider_id"],
                )
                if p
            }
            <= provider_ids
        ):
            raise ValueError("Tenant provider configuration is incomplete")
        for provider in config["provider"]:
            if provider["id"] == self.providers["chat_provider_id"]:
                provider["key"] = [model_key]
                provider["api_base"] = self.proxy_url
            if provider["id"] == self.providers["embedding_provider_id"]:
                provider["embedding_api_key"] = model_key
                provider["embedding_api_base"] = self.proxy_url
        temp = config_path.with_suffix(".tmp")
        temp.write_text(json.dumps(config, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(config_path)
        if not plugin_path.exists():
            plugin_path.write_text(
                json.dumps(
                    {
                        "enabled": community_enabled,
                        "platform_id": f"tenant-{bot_id}",
                        "owner_uid": str(row["owner_id"]),
                        "chat_provider_id": self.providers["chat_provider_id"],
                        "embedding_provider_id": self.providers[
                            "embedding_provider_id"
                        ],
                        "usdt_enabled": False,
                        "keno_fetch_enabled": False,
                        "chat_only_tenant": not bool(
                            self.providers["embedding_provider_id"]
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            plugin_path.chmod(0o600)
        return data, token_changed

    def reconcile(self, bot_id: int, wait_health: bool = False) -> None:
        """Start, resume, or suspend exactly one verified tenant instance."""
        row = self.store.db.execute(
            "SELECT * FROM bots WHERE id=?", (bot_id,)
        ).fetchone()
        if not row:
            raise ValueError("Unknown tenant")
        state = self._state(bot_id)
        now = int(time.time())
        if row["expires_at"] <= now or not row["enabled"]:
            if state == "running":
                self._docker("stop", self._container(bot_id))
            self.store.db.execute(
                "UPDATE bots SET status=? WHERE id=?",
                (
                    "archived"
                    if row["expires_at"] <= now - 30 * 86400
                    else "paused"
                    if not row["enabled"]
                    else "suspended",
                    bot_id,
                ),
            )
            return
        if state == "missing" and not self.proxy_ready():
            raise ValueError("Tenant model gateway is unavailable")
        data, token_changed = self._prepare(row)
        if state != "running" or token_changed:
            (data / "tenant-health.json").unlink(missing_ok=True)
        if state == "missing":
            self._docker(
                "run",
                "-d",
                "--name",
                self._container(bot_id),
                "--label",
                f"tenant-control.bot-id={bot_id}",
                "--restart",
                "unless-stopped",
                "--security-opt",
                "no-new-privileges:true",
                "--cap-drop",
                "ALL",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--pids-limit",
                "128",
                "--memory",
                "1g",
                "--cpus",
                "1",
                "--add-host",
                "host.docker.internal:host-gateway",
                "-v",
                f"{data}:/AstrBot/data:rw",
                self.image,
            )
        elif state == "stopped":
            self._docker("start", self._container(bot_id))
        elif token_changed:
            self._docker("restart", self._container(bot_id))
        healthy = self._healthy(bot_id)
        if wait_health:
            for _ in range(30):
                if healthy:
                    break
                time.sleep(2)
                healthy = self._healthy(bot_id)
        if not healthy:
            raise ValueError("Tenant Telegram transport is not healthy yet")
        self.store.db.execute("UPDATE bots SET status='active' WHERE id=?", (bot_id,))
        self.store.db.execute(
            "UPDATE orders SET status='active' WHERE bot_id=? AND status='provisioning'",
            (bot_id,),
        )

    def run_once(self) -> bool:
        """Claim one durable job and back off on errors; return if work was found."""
        if self.run_operation():
            return True
        now = int(time.time())
        active = self.store.db.execute(
            "SELECT id FROM bots WHERE status='active' AND enabled=1 AND expires_at>?",
            (now,),
        ).fetchall()
        stopped = [row["id"] for row in active if self._state(row["id"]) != "running"]
        with self.store.tx() as db:
            for bot_id in stopped:
                self.store._enqueue(db, bot_id, "reconcile")
            for row in db.execute(
                "SELECT id FROM bots WHERE "
                "(status='active' AND expires_at<=?) OR "
                "(status='suspended' AND expires_at<=?)",
                (now, now - 30 * 86400),
            ):
                db.execute(
                    "UPDATE jobs SET state='pending',retry_at=0 "
                    "WHERE bot_id=? AND kind='reconcile' AND state='done'",
                    (row["id"],),
                )
            job = db.execute(
                "SELECT * FROM jobs WHERE state IN ('pending','failed') AND retry_at<=? "
                "ORDER BY id LIMIT 1",
                (now,),
            ).fetchone()
            if not job:
                return False
            db.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?",
                (job["id"],),
            )
        try:
            self.reconcile(job["bot_id"])
        except Exception as error:
            self.store.db.execute(
                "UPDATE jobs SET state='failed',retry_at=?,error=? WHERE id=?",
                (
                    now + min(3600, 2 ** min(job["attempts"] + 1, 12)),
                    type(error).__name__,
                    job["id"],
                ),
            )
            return True
        self.store.db.execute(
            "UPDATE jobs SET state='done',error='' WHERE id=?", (job["id"],)
        )
        return True

    def run_operation(self) -> bool:
        """Run only confirmed, fixed operations; a crash requires human review."""
        with self.store.tx() as db:
            item = db.execute(
                "SELECT * FROM operations WHERE state='pending' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not item:
                return False
            db.execute(
                "UPDATE operations SET state='running' WHERE id=?", (item["id"],)
            )
        result = ""
        try:
            action, bot_id = item["action"], item["bot_id"]
            if action in {"pause", "resume"}:
                self.store.set_bot_enabled(bot_id, action == "resume", item["actor"])
                self.reconcile(bot_id, wait_health=action == "resume")
            elif action == "backup":
                result = self.backup(bot_id).name
            elif action == "restore":
                self.restore(bot_id, item["argument"])
            elif action == "upgrade":
                result = self.upgrade_plugin(bot_id).name
            else:
                raise ValueError("Unsupported operation")
            state = "done"
        except Exception as error:
            state, result = "needs_review", type(error).__name__
        with self.store.tx() as db:
            db.execute(
                "UPDATE operations SET state=?,result=? WHERE id=?",
                (state, result, item["id"]),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (
                    int(time.time()),
                    str(item["actor"]),
                    item["action"] + "_" + state,
                    item["id"],
                ),
            )
        return True

    def upgrade_plugin(self, bot_id: int, custom_approved: bool = False) -> Path:
        """Roll out reviewed base plugin to one tenant with a preserved backup.

        Args:
            bot_id: Target Bot ID.
            custom_approved: Operator-confirmed compatibility for tenant extensions.

        Returns:
            Snapshot available for rollback.
        """
        bot = self.store.db.execute(
            "SELECT 1 FROM bots WHERE id=?", (bot_id,)
        ).fetchone()
        if not bot:
            raise ValueError("Unknown bot")
        data = self.root / str(bot_id) / "data"
        plugin = data / "plugins" / "astrbot_plugin_superbot"
        source = self.source / "data/plugins/astrbot_plugin_superbot"
        if not plugin.is_dir() or not source.is_dir():
            raise ValueError("Base plugin is missing")
        if (
            any((data / "plugins").glob("astrbot_plugin_custom_*"))
            and not custom_approved
        ):
            raise ValueError("Tenant extension compatibility must be approved")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("Untrusted source")
        snapshot = self.backup(bot_id)
        staged = self.root / str(bot_id) / f".base-plugin-{secrets.token_hex(8)}"
        shutil.copytree(
            source,
            staged,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        state = self._state(bot_id)
        if state == "running":
            self._docker("stop", self._container(bot_id))
        previous = snapshot / "previous-base-plugin"
        try:
            plugin.rename(previous)
            staged.rename(plugin)
            if state == "running":
                (data / "tenant-health.json").unlink(missing_ok=True)
                self._docker("start", self._container(bot_id))
                self.reconcile(bot_id, wait_health=True)
        except BaseException:
            if state == "running":
                self._docker("stop", self._container(bot_id))
            if plugin.exists():
                shutil.rmtree(plugin)
            if previous.exists():
                previous.rename(plugin)
            if state == "running":
                self._docker("start", self._container(bot_id))
            raise
        return snapshot

    @staticmethod
    def _extract_extension(archive: Path, staged: Path) -> None:
        """Validate and extract a plugin ZIP without following archive paths.

        Args:
            archive: Operator-provided ZIP.
            staged: Fresh target directory.
        """
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if (
                not names
                or len(names) != len(set(names))
                or "metadata.yaml" not in names
                or "main.py" not in names
                or sum(info.file_size for info in bundle.infolist()) > 20 * 1024 * 1024
            ):
                raise ValueError(
                    "Plugin ZIP must have metadata.yaml and main.py at root"
                )
            for info in bundle.infolist():
                name = Path(info.filename)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                    or info.file_size > 4 * 1024 * 1024
                ):
                    raise ValueError("Unsafe plugin archive")
            staged.mkdir(mode=0o700)
            try:
                for info in bundle.infolist():
                    output = staged / info.filename
                    if info.is_dir():
                        output.mkdir(parents=True, exist_ok=True, mode=0o700)
                    else:
                        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with bundle.open(info) as src, output.open("xb") as dst:
                            shutil.copyfileobj(src, dst)
                        output.chmod(0o600)
            except BaseException:
                shutil.rmtree(staged)
                raise

    def stage_extension(self, bot_id: int, order_id: str, archive: Path) -> Path:
        """Prepare private test data with Telegram and platform models disabled.

        Args:
            bot_id: Customer's existing tenant.
            order_id: Paid custom work.
            archive: Reviewed ZIP to test and later install.

        Returns:
            Private staging data directory for network-isolated operator tests.
        """
        order = self.store.db.execute(
            "SELECT 1 FROM orders WHERE id=? AND bot_id=? "
            "AND tier='custom' AND status='paid'",
            (order_id, bot_id),
        ).fetchone()
        if not order:
            raise ValueError("Paid custom quote required")
        staged = self.root / str(bot_id) / "staging" / order_id
        if staged.exists():
            raise ValueError("This order already has a staged archive")
        snapshot = self.backup(bot_id)
        staged.parent.mkdir(mode=0o700, exist_ok=True)
        shutil.copytree(snapshot, staged)
        try:
            config = json.loads(
                (staged / "cmd_config.json").read_text(encoding="utf-8-sig")
            )
            config["platform"] = []
            config["provider"] = []
            config["admins_id"] = []
            config["dashboard"]["enable"] = False
            config["dashboard"]["password"] = ""
            config["dashboard"]["pbkdf2_password"] = ""
            config["dashboard"]["jwt_secret"] = ""
            config["plugin_set"] = [f"astrbot_plugin_custom_{bot_id}"]
            (staged / "cmd_config.json").write_text(json.dumps(config))
            plugin_config = staged / "config/astrbot_plugin_superbot_config.json"
            if plugin_config.exists():
                settings = json.loads(plugin_config.read_text(encoding="utf-8-sig"))
                settings["enabled"] = False
                settings["usdt_enabled"] = False
                settings["keno_fetch_enabled"] = False
                plugin_config.write_text(json.dumps(settings))
            candidate = staged / "plugins" / f"astrbot_plugin_custom_{bot_id}"
            if candidate.exists():
                shutil.rmtree(candidate)
            self._extract_extension(archive, candidate)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO extension_reviews(order_id,bot_id,digest,status) "
                    "VALUES(?,?,?,'staged')",
                    (order_id, bot_id, digest),
                )
        except BaseException:
            shutil.rmtree(staged)
            raise
        return staged

    def approve_extension(self, bot_id: int, order_id: str, archive: Path) -> None:
        """Attest acceptance of the exact staged ZIP after offline tenant tests.

        Args:
            bot_id: Customer tenant tested.
            order_id: Paid custom quote.
            archive: Same ZIP that was staged.
        """
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.store.tx() as db:
            review = db.execute(
                "SELECT digest,status FROM extension_reviews "
                "WHERE order_id=? AND bot_id=?",
                (order_id, bot_id),
            ).fetchone()
            if not review or review["status"] != "staged" or review["digest"] != digest:
                raise ValueError("Staged extension hash mismatch or missing review")
            db.execute(
                "UPDATE extension_reviews SET status='approved',reviewed_at=? "
                "WHERE order_id=?",
                (int(time.time()), order_id),
            )
            db.execute(
                "INSERT INTO audit(at,actor,action,ref) VALUES(?,?,?,?)",
                (int(time.time()), "operator", "extension_approved", order_id),
            )

    def install_extension(self, bot_id: int, order_id: str, archive: Path) -> None:
        """Install a paid and approved tenant-only plugin after backup.

        Args:
            bot_id: Existing tenant Bot ID.
            order_id: Paid custom quote reference.
            archive: Same ZIP that passed staging review.
        """
        order = self.store.db.execute(
            "SELECT 1 FROM orders WHERE id=? AND bot_id=? AND tier='custom' AND status='paid'",
            (order_id, bot_id),
        ).fetchone()
        if not order:
            raise ValueError("Paid custom quote required")
        review = self.store.db.execute(
            "SELECT 1 FROM extension_reviews WHERE order_id=? AND bot_id=? "
            "AND digest=? AND status='approved'",
            (order_id, bot_id, hashlib.sha256(archive.read_bytes()).hexdigest()),
        ).fetchone()
        if not review:
            raise ValueError("Approved staged extension required")
        target = (
            self.root
            / str(bot_id)
            / "data"
            / "plugins"
            / f"astrbot_plugin_custom_{bot_id}"
        )
        backup = self.backup(bot_id)
        staged = self.root / str(bot_id) / f".extension-{secrets.token_hex(8)}"
        self._extract_extension(archive, staged)
        state = self._state(bot_id)
        old = backup / "previous-extension"
        if state == "running":
            self._docker("stop", self._container(bot_id))
        try:
            if target.exists():
                target.rename(old)
            staged.rename(target)
            if state == "running":
                (self.root / str(bot_id) / "data/tenant-health.json").unlink(
                    missing_ok=True
                )
                self._docker("start", self._container(bot_id))
                self.reconcile(bot_id, wait_health=True)
        except BaseException:
            if state == "running":
                self._docker("stop", self._container(bot_id))
            if target.exists():
                shutil.rmtree(target)
            if old.exists():
                old.rename(target)
            if state == "running":
                self._docker("start", self._container(bot_id))
            raise
        self.store.db.execute(
            "UPDATE orders SET status='active' WHERE id=?", (order_id,)
        )
        with self.store.tx() as db:
            self.store._enqueue(db, bot_id, "reconcile")

    def backup(self, bot_id: int) -> Path:
        """Snapshot tenant config, plugin code, and databases without WAL races."""
        source = self.root / str(bot_id) / "data"
        if not source.is_dir():
            raise ValueError("Tenant data does not exist")
        backup = self.root / str(bot_id) / "backups" / str(time.time_ns())
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path in source.rglob("*"):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.name.endswith(("-wal", "-shm"))
            ):
                continue
            target = backup / path.relative_to(source)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.suffix in {".db", ".sqlite3"}:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as src:
                    with sqlite3.connect(target) as dst:
                        src.backup(dst)
            else:
                shutil.copyfile(path, target)
            target.chmod(0o600)
        return backup

    def restore(self, bot_id: int, snapshot: str) -> None:
        """Restore an operator-selected local snapshot after stopping the bot.

        Args:
            bot_id: Tenant Bot ID.
            snapshot: Numeric snapshot directory name, not an arbitrary path.
        """
        if not snapshot.isdigit():
            raise ValueError("Snapshot name must be numeric")
        tenant = self.root / str(bot_id)
        backup = tenant / "backups" / snapshot
        if not (backup / "cmd_config.json").is_file():
            raise ValueError("Snapshot is missing tenant configuration")
        staging = tenant / f".restore-{secrets.token_hex(8)}"
        shutil.copytree(
            backup,
            staging,
            ignore=shutil.ignore_patterns("previous-extension", "previous-base-plugin"),
        )
        state = self._state(bot_id)
        if state == "running":
            self._docker("stop", self._container(bot_id))
        old = tenant / f"data.before-restore-{time.time_ns()}"
        try:
            (tenant / "data").rename(old)
            staging.rename(tenant / "data")
            self.reconcile(bot_id, wait_health=True)
        except BaseException:
            if self._state(bot_id) == "running":
                self._docker("stop", self._container(bot_id))
            if (tenant / "data").exists():
                (tenant / "data").rename(
                    tenant / f"data.failed-restore-{time.time_ns()}"
                )
            if old.exists():
                old.rename(tenant / "data")
            if state == "running":
                self._docker("start", self._container(bot_id))
            raise
